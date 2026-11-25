import * as THREE from 'three';

const VERTEX_SHADER = /* glsl */ `
  #include <fog_pars_vertex>

  uniform float uVelocity;
  uniform float uActive;
  uniform float uHover;

  varying vec2 vUv;

  void main() {
    vUv = uv;
    vec3 pos = position;

    // Subtle velocity-proportional optical bend that damps back to zero at rest.
    float radial = length(uv - 0.5);
    float bend = clamp(uVelocity, -1.5, 1.5) * 0.14;
    pos.z -= (radial * radial) * bend;
    pos.y += (uv.x - 0.5) * bend * 0.08;

    // Gentle forward breathe when active or hovered.
    pos.z += (uActive * 0.18) + (uHover * 0.12);

    vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
    gl_Position = projectionMatrix * mvPosition;

    #include <fog_vertex>
  }
`;

const FRAGMENT_SHADER = /* glsl */ `
  #include <fog_pars_fragment>

  uniform sampler2D uTexture;
  uniform vec2 uParallax;
  uniform vec2 uResolution;
  uniform float uVelocity;
  uniform float uActive;
  uniform float uHover;

  varying vec2 vUv;

  void main() {
    // Recess the image inside the frame with subtle inverse parallax.
    vec2 centered = vUv - 0.5;
    vec2 parallaxOffset = uParallax * 0.032;
    vec2 uvBase = 0.5 + centered * 0.92 - parallaxOffset;

    // Micro-chromatic aberration proportional to scroll velocity.
    float chroma = abs(uVelocity) * 0.0055;
    vec2 dir = normalize(centered + vec2(0.0001));

    float r = texture2D(uTexture, clamp(uvBase + dir * chroma, 0.002, 0.998)).r;
    float g = texture2D(uTexture, clamp(uvBase, 0.002, 0.998)).g;
    float b = texture2D(uTexture, clamp(uvBase - dir * chroma, 0.002, 0.998)).b;

    vec3 color = vec3(r, g, b);

    // Subtle cinema darkroom vignette + presence lift when active/hovered.
    float dist = length(centered);
    float vignette = smoothstep(0.88, 0.25, dist);
    float presence = mix(0.74, 1.06, clamp(uActive * 0.8 + uHover * 0.45, 0.0, 1.0));
    color *= mix(0.72, 1.0, vignette) * presence;

    // Crisp 1px-style architectural hairline border on the slab perimeter.
    vec2 edgeDist = min(vUv, 1.0 - vUv) * uResolution;
    float borderPx = 1.25;
    float borderMask = 1.0 - smoothstep(0.0, borderPx, min(edgeDist.x, edgeDist.y));

    vec3 idleBorder = vec3(0.95, 0.94, 0.92) * 0.22;
    vec3 activeBorder = vec3(0.90, 0.28, 0.18); // restrained vermilion
    vec3 hoverBorder = vec3(0.95, 0.94, 0.92) * 0.85;

    vec3 borderColor = mix(idleBorder, activeBorder, uActive);
    borderColor = mix(borderColor, hoverBorder, uHover * 0.65);

    color = mix(color, borderColor, borderMask * 0.92);

    gl_FragColor = vec4(color, 1.0);

    #include <fog_fragment>
  }
`;

export interface DioramaUniforms {
  [uniform: string]: THREE.IUniform<unknown>;
  uTexture: THREE.IUniform<THREE.Texture>;
  uParallax: THREE.IUniform<THREE.Vector2>;
  uResolution: THREE.IUniform<THREE.Vector2>;
  uVelocity: THREE.IUniform<number>;
  uActive: THREE.IUniform<number>;
  uHover: THREE.IUniform<number>;
  fogColor: THREE.IUniform<THREE.Color>;
  fogDensity: THREE.IUniform<number>;
}

export function createDioramaMaterial(
  texture: THREE.Texture,
  width: number,
  height: number,
): THREE.ShaderMaterial {
  const uniforms: DioramaUniforms = {
    uTexture: { value: texture },
    uParallax: { value: new THREE.Vector2(0, 0) },
    uResolution: { value: new THREE.Vector2(width * 180, height * 180) },
    uVelocity: { value: 0 },
    uActive: { value: 0 },
    uHover: { value: 0 },
    fogColor: { value: new THREE.Color('#09090B') },
    fogDensity: { value: 0.032 },
  };

  return new THREE.ShaderMaterial({
    uniforms,
    vertexShader: VERTEX_SHADER,
    fragmentShader: FRAGMENT_SHADER,
    fog: true,
    side: THREE.FrontSide,
  });
}

