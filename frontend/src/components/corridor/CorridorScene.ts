import * as THREE from 'three';
import { createDioramaMaterial } from './dioramaShader';
import type { CorridorMediaItem } from './mediaCatalog';

export interface CorridorCallbacks {
  onProgress?: (progress: number) => void;
  onActiveChange?: (index: number, zDepth: number) => void;
  onSelectItem?: (item: CorridorMediaItem) => void;
}

interface SlabNode {
  item: CorridorMediaItem;
  mesh: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>;
  geometry: THREE.PlaneGeometry;
  material: THREE.ShaderMaterial;
  texture: THREE.Texture;
}

const BG_HEX = '#09090B';
const FOG_DENSITY = 0.032;
const CULL_DISTANCE = 42;
const ACTIVE_THRESHOLD = 9.5;
const ENTRY_Z = 10;
const PRELOAD_Z = 14;

export class CorridorScene {
  private container: HTMLElement;
  private items: readonly CorridorMediaItem[];
  private readonly callbacks: CorridorCallbacks;

  private readonly renderer: THREE.WebGLRenderer;
  private readonly scene: THREE.Scene;
  private readonly camera: THREE.PerspectiveCamera;
  private readonly spline: THREE.CatmullRomCurve3;
  private readonly raycaster = new THREE.Raycaster();
  private readonly pointerNDC = new THREE.Vector2(0, 0);
  private readonly pointerTarget = new THREE.Vector2(0, 0);
  private readonly pointerSmooth = new THREE.Vector2(0, 0);
  private readonly lookAtTarget = new THREE.Vector3(0, 0, 0);
  private readonly lookAtSmooth = new THREE.Vector3(0, 0, 0);
  private readonly curvePos = new THREE.Vector3();
  private readonly curveAhead = new THREE.Vector3();

  private readonly slabs: SlabNode[] = [];
  private readonly resizeObserver: ResizeObserver;

  private rafId: number | null = null;
  private lastTime = 0;
  private disposed = false;

  private targetProgress = 0;
  private smoothProgress = 0;
  private scrollVelocity = 0;
  private smoothVelocity = 0;
  private hoveredId: string | null = null;
  private activeIndex = 0;
  private lastReportedZ = PRELOAD_Z;

  /** Controlled by PreloaderOverture during Z-space handoff (`z = +26` -> `+10`). */
  private dollyZ = PRELOAD_Z;
  private isHandedOff = false;
  private swapToken = 0;

  constructor(
    container: HTMLElement,
    items: readonly CorridorMediaItem[],
    callbacks: CorridorCallbacks = {},
  ) {
    this.container = container;
    this.items = items;
    this.callbacks = callbacks;

    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 1);

    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      powerPreference: 'high-performance',
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(width, height);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.04;
    this.renderer.domElement.style.display = 'block';
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(BG_HEX);
    this.scene.fog = new THREE.FogExp2(BG_HEX, FOG_DENSITY);

    this.camera = new THREE.PerspectiveCamera(44, width / height, 0.1, 120);
    this.camera.position.set(0, 0, PRELOAD_Z);
    this.camera.lookAt(0, 0, 0);

    // Gentle weaving spline from z = +10 down to z = -140.
    this.spline = new THREE.CatmullRomCurve3(
      [
        new THREE.Vector3(0, 0, ENTRY_Z),
        new THREE.Vector3(-0.7, 0.15, -6),
        new THREE.Vector3(0.8, -0.15, -24),
        new THREE.Vector3(-0.9, 0.2, -46),
        new THREE.Vector3(0.85, -0.1, -68),
        new THREE.Vector3(-0.75, 0.15, -92),
        new THREE.Vector3(0.65, -0.15, -116),
        new THREE.Vector3(0, 0, -140),
      ],
      false,
      'catmullrom',
      0.5,
    );

    this.resizeObserver = new ResizeObserver((entries) => {
      if (this.disposed) return;
      const entry = entries[0];
      if (!entry) return;
      const w = Math.max(entry.contentRect.width, 1);
      const h = Math.max(entry.contentRect.height, 1);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      this.renderer.setSize(w, h);
    });
    this.resizeObserver.observe(container);

    window.addEventListener('pointermove', this.handlePointerMove, { passive: true });
    window.addEventListener('pointerleave', this.handlePointerReset);
    window.addEventListener('blur', this.handlePointerReset);
    this.renderer.domElement.addEventListener('click', this.handleClick);
  }

  /**
   * Deterministic preloader preparation:
   * 1. Load all slab textures via THREE.LoadingManager (mipmaps disabled)
   * 2. Wait for document.fonts.ready
   * 3. Pre-compile shaders with renderer.compileAsync(scene, camera)
   */
  async prepare(): Promise<void> {
    if (this.items.length === 0) {
      await document.fonts.ready;
      this.callbacks.onProgress?.(1);
      this.startLoop();
      return;
    }
    const manager = new THREE.LoadingManager();
    const totalItems = this.items.length;

    const texturesLoaded = new Promise<void>((resolve) => {
      let settled = false;
      const done = () => {
        if (!settled) {
          settled = true;
          resolve();
        }
      };

      manager.onProgress = (_url, loaded, total) => {
        const ratio = total > 0 ? loaded / total : 1;
        this.callbacks.onProgress?.(ratio * 0.85);
      };

      manager.onLoad = () => {
        this.callbacks.onProgress?.(0.88);
        done();
      };

      manager.onError = () => {
        // Never hang the preloader if a single asset fails.
        if (totalItems === 0) done();
      };

      const loader = new THREE.TextureLoader(manager);
      loader.setCrossOrigin('anonymous');
      for (const item of this.items) {
        const tex = loader.load(item.imageSrc, undefined, undefined, () => {
          // TextureLoader calls onError on manager automatically.
        });
        tex.generateMipmaps = false;
        tex.minFilter = THREE.LinearFilter;
        tex.magFilter = THREE.LinearFilter;
        tex.colorSpace = THREE.SRGBColorSpace;
        tex.wrapS = THREE.ClampToEdgeWrapping;
        tex.wrapT = THREE.ClampToEdgeWrapping;

        const geometry = new THREE.PlaneGeometry(item.size.width, item.size.height, 16, 12);
        const material = createDioramaMaterial(tex, item.size.width, item.size.height);
        const mesh = new THREE.Mesh(geometry, material);
        mesh.position.set(item.position.x, item.position.y, item.position.z);
        mesh.userData.mediaId = item.id;
        this.scene.add(mesh);

        this.slabs.push({ item, mesh, geometry, material, texture: tex });
      }
    });

    await Promise.all([texturesLoaded, document.fonts.ready]);
    if (this.disposed) return;

    this.callbacks.onProgress?.(0.94);

    // Ensure every slab is visible during shader pre-compilation, then restore culling.
    for (const slab of this.slabs) slab.mesh.visible = true;
    await this.renderer.compileAsync(this.scene, this.camera);
    if (this.disposed) return;

    this.callbacks.onProgress?.(1);
    this.startLoop();
  }

  /** Swap the corridor slabs when switching between Trending and Archival categories. */
  async swapItems(nextItems: readonly CorridorMediaItem[]): Promise<void> {
    if (this.disposed) return;
    const token = ++this.swapToken;
    for (const slab of this.slabs) {
      this.scene.remove(slab.mesh);
      slab.geometry.dispose();
      slab.material.dispose();
      slab.texture.dispose();
    }
    this.slabs.length = 0;
    this.items = nextItems;
    this.hoveredId = null;

    const loader = new THREE.TextureLoader();
    loader.setCrossOrigin('anonymous');
    const created: SlabNode[] = [];
    await Promise.all(
      nextItems.map(
        (item) =>
          new Promise<void>((resolve) => {
            const tex = loader.load(
              item.imageSrc,
              () => resolve(),
              undefined,
              () => resolve(),
            );
            tex.generateMipmaps = false;
            tex.minFilter = THREE.LinearFilter;
            tex.magFilter = THREE.LinearFilter;
            tex.colorSpace = THREE.SRGBColorSpace;
            tex.wrapS = THREE.ClampToEdgeWrapping;
            tex.wrapT = THREE.ClampToEdgeWrapping;

            const geometry = new THREE.PlaneGeometry(item.size.width, item.size.height, 16, 12);
            const material = createDioramaMaterial(tex, item.size.width, item.size.height);
            const mesh = new THREE.Mesh(geometry, material);
            mesh.position.set(item.position.x, item.position.y, item.position.z);
            mesh.userData.mediaId = item.id;
            created.push({ item, mesh, geometry, material, texture: tex });
          }),
      ),
    );
    if (this.disposed || token !== this.swapToken) {
      for (const s of created) {
        s.geometry.dispose();
        s.material.dispose();
        s.texture.dispose();
      }
      return;
    }
    for (const slab of created) {
      slab.mesh.visible = true;
      this.scene.add(slab.mesh);
      this.slabs.push(slab);
    }
    await this.renderer.compileAsync(this.scene, this.camera);
  }

  /** Called by PreloaderOverture tween to dolly from z = +26 to z = +10. */
  setDollyZ(z: number): void {
    this.dollyZ = z;
  }

  /** Unlocks spline-driven camera flight after the Z-space handoff completes. */
  completeHandoff(): void {
    this.isHandedOff = true;
    this.dollyZ = ENTRY_Z;
  }

  /** Normalized scroll progress [0, 1] from ScrollTrigger. */
  setTargetProgress(progress: number, velocityDelta = 0): void {
    this.targetProgress = THREE.MathUtils.clamp(progress, 0, 1);
    if (Math.abs(velocityDelta) > 0.0001) {
      this.scrollVelocity = THREE.MathUtils.clamp(velocityDelta, -2.5, 2.5);
    }
  }

  private handlePointerMove = (e: PointerEvent): void => {
    const rect = this.container.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    const ny = -(((e.clientY - rect.top) / rect.height) * 2 - 1);
    this.pointerNDC.set(nx, ny);
    this.pointerTarget.set(nx, ny);
  };

  private handlePointerReset = (): void => {
    this.pointerTarget.set(0, 0);
  };

  private handleClick = (): void => {
    if (!this.isHandedOff) return;
    this.raycaster.setFromCamera(this.pointerNDC, this.camera);
    const visibleMeshes = this.slabs.filter((s) => s.mesh.visible).map((s) => s.mesh);
    const hits = this.raycaster.intersectObjects(visibleMeshes, false);
    if (hits.length === 0) return;
    const hitId = hits[0].object.userData.mediaId as string | undefined;
    const match = this.slabs.find((s) => s.item.id === hitId);
    if (match) {
      this.callbacks.onSelectItem?.(match.item);
    }
  };

  private startLoop(): void {
    if (this.rafId !== null || this.disposed) return;
    this.lastTime = performance.now();
    const tick = (now: number) => {
      if (this.disposed) return;
      const dt = Math.min((now - this.lastTime) / 1000, 0.05);
      this.lastTime = now;
      this.update(dt);
      this.renderer.render(this.scene, this.camera);
      this.rafId = requestAnimationFrame(tick);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  private update(dt: number): void {
    this.pointerSmooth.x = THREE.MathUtils.damp(this.pointerSmooth.x, this.pointerTarget.x, 5.5, dt);
    this.pointerSmooth.y = THREE.MathUtils.damp(this.pointerSmooth.y, this.pointerTarget.y, 5.5, dt);

    if (!this.isHandedOff) {
      this.camera.position.set(
        this.pointerSmooth.x * 0.25,
        this.pointerSmooth.y * 0.16,
        this.dollyZ,
      );
      this.camera.lookAt(0, 0, -8);
    } else {
      const prevSmooth = this.smoothProgress;
      this.smoothProgress = THREE.MathUtils.damp(this.smoothProgress, this.targetProgress, 6.0, dt);
      const instantVel = (this.smoothProgress - prevSmooth) / Math.max(dt, 0.001) * 12;
      this.scrollVelocity = THREE.MathUtils.damp(this.scrollVelocity, instantVel, 8.0, dt);
      this.smoothVelocity = THREE.MathUtils.damp(this.smoothVelocity, this.scrollVelocity, 7.5, dt);

      this.spline.getPointAt(this.smoothProgress, this.curvePos);
      const aheadT = Math.min(this.smoothProgress + 0.045, 1);
      this.spline.getPointAt(aheadT, this.curveAhead);

      this.camera.position.set(
        this.curvePos.x + this.pointerSmooth.x * 0.45,
        this.curvePos.y + this.pointerSmooth.y * 0.28,
        this.curvePos.z,
      );

      this.lookAtTarget.set(
        this.curveAhead.x + this.pointerSmooth.x * 0.18,
        this.curveAhead.y + this.pointerSmooth.y * 0.12,
        this.curveAhead.z - 6,
      );
      this.lookAtSmooth.lerp(this.lookAtTarget, 1 - Math.exp(-6 * dt));
      this.camera.lookAt(this.lookAtSmooth);
    }

    // Raycast for hover state only after handoff.
    let nextHoveredId: string | null = null;
    if (this.isHandedOff) {
      this.raycaster.setFromCamera(this.pointerNDC, this.camera);
      const visibleMeshes = this.slabs.filter((s) => s.mesh.visible).map((s) => s.mesh);
      const hits = this.raycaster.intersectObjects(visibleMeshes, false);
      if (hits.length > 0) {
        nextHoveredId = (hits[0].object.userData.mediaId as string) ?? null;
      }
    }
    if (nextHoveredId !== this.hoveredId) {
      this.hoveredId = nextHoveredId;
      this.renderer.domElement.style.cursor = nextHoveredId ? 'pointer' : 'default';
    }

    // Distance culling + proximity-synced slab uniforms.
    let closestIdx = this.activeIndex;
    let closestDist = Infinity;

    for (let i = 0; i < this.slabs.length; i++) {
      const slab = this.slabs[i];
      const dz = Math.abs(slab.mesh.position.z - this.camera.position.z);
      slab.mesh.visible = dz < CULL_DISTANCE;

      if (dz < closestDist) {
        closestDist = dz;
        closestIdx = i;
      }

      if (!slab.mesh.visible) continue;

      const isActive = dz < ACTIVE_THRESHOLD ? 1 - dz / ACTIVE_THRESHOLD : 0;
      const isHovered = slab.item.id === this.hoveredId ? 1 : 0;

      const uniforms = slab.material.uniforms;
      uniforms.uActive.value = THREE.MathUtils.damp(uniforms.uActive.value, isActive, 6.5, dt);
      uniforms.uHover.value = THREE.MathUtils.damp(uniforms.uHover.value, isHovered, 8.0, dt);
      uniforms.uVelocity.value = this.smoothVelocity;
      uniforms.uParallax.value.set(
        this.pointerSmooth.x * 0.8 + this.smoothVelocity * 0.12,
        this.pointerSmooth.y * 0.8,
      );
    }

    const zRounded = Math.round(this.camera.position.z * 10) / 10;
    if (closestIdx !== this.activeIndex || Math.abs(zRounded - this.lastReportedZ) >= 0.2) {
      this.activeIndex = closestIdx;
      this.lastReportedZ = zRounded;
      this.callbacks.onActiveChange?.(closestIdx, zRounded);
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.resizeObserver.disconnect();
    window.removeEventListener('pointermove', this.handlePointerMove);
    window.removeEventListener('pointerleave', this.handlePointerReset);
    window.removeEventListener('blur', this.handlePointerReset);
    this.renderer.domElement.removeEventListener('click', this.handleClick);

    for (const slab of this.slabs) {
      this.scene.remove(slab.mesh);
      slab.geometry.dispose();
      slab.material.dispose();
      slab.texture.dispose();
    }
    this.slabs.length = 0;

    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

