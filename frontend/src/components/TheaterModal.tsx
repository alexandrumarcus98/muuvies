import { useCallback, useEffect, useRef, useState } from 'react';
import type HlsType from 'hls.js';
import { ApiError, apiFetch } from '../lib/api';
import type { StreamManifestResponse } from '../types/api';
import { useBackdropDismiss, useDialogEnter } from './Modal';

interface HlsTrack {
  id: number;
  name?: string;
  lang?: string;
}

interface TheaterModalProps {
  itemId: string | null;
  title: string;
  prefetchedManifest?: StreamManifestResponse | null;
  onClose: () => void;
  onProgressSaved: () => void;
}

function parseTimestampSeconds(ts: string): number {
  const clean = ts.trim().replace(',', '.');
  const parts = clean.split(':');
  if (parts.length === 3) {
    return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
  }
  if (parts.length === 2) {
    return Number(parts[0]) * 60 + Number(parts[1]);
  }
  return Number(clean) || 0;
}

function formatVttTimestamp(seconds: number): string {
  const clamped = Math.max(0, seconds);
  const hrs = Math.floor(clamped / 3600);
  const mins = Math.floor((clamped % 3600) / 60);
  const secs = Math.floor(clamped % 60);
  const ms = Math.round((clamped - Math.floor(clamped)) * 1000);
  const pad2 = (n: number) => String(n).padStart(2, '0');
  const pad3 = (n: number) => String(n).padStart(3, '0');
  return `${pad2(hrs)}:${pad2(mins)}:${pad2(secs)}.${pad3(ms)}`;
}

function buildShiftedVtt(rawText: string, offsetSeconds: number): string {
  const normalized = rawText.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const lines = normalized.split('\n');
  const out: string[] = [];
  const hasHeader = lines[0]?.trim().startsWith('WEBVTT');
  if (!hasHeader) {
    out.push('WEBVTT', '');
  }

  const timeRangeRe = /^(\d{1,2}:?\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d{1,2}:?\d{2}:\d{2}[.,]\d{3})(.*)$/;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const match = line.match(timeRangeRe);
    if (match) {
      const start = parseTimestampSeconds(match[1]) + offsetSeconds;
      const end = parseTimestampSeconds(match[2]) + offsetSeconds;
      out.push(`${formatVttTimestamp(start)} --> ${formatVttTimestamp(Math.max(start, end))}${match[3] || ''}`);
    } else {
      out.push(line);
    }
  }
  return out.join('\n');
}

export default function TheaterModal({
  itemId,
  title,
  prefetchedManifest,
  onClose,
  onProgressSaved,
}: TheaterModalProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<HlsType | null>(null);
  const lastSavedAtRef = useRef<number>(0);

  const [manifest, setManifest] = useState<StreamManifestResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [usingDirectFallback, setUsingDirectFallback] = useState(false);
  // Direct play decoded video but no audio (Dolby/DTS tracks browsers can't decode).
  const [silentAudio, setSilentAudio] = useState(false);
  const [waitingNote, setWaitingNote] = useState<string | null>(null);

  const [audioTracks, setAudioTracks] = useState<HlsTrack[]>([]);
  const [activeAudioTrack, setActiveAudioTrack] = useState<number>(0);

  const [subtitleRaw, setSubtitleRaw] = useState<string | null>(null);
  const [subtitleLabel, setSubtitleLabel] = useState<string>('');
  const [subtitleOffset, setSubtitleOffset] = useState<number>(0);
  const [subtitleUrl, setSubtitleUrl] = useState<string | null>(null);

  const saveProgress = useCallback(
    async (force = false, useKeepalive = false) => {
      if (!itemId) return;
      const video = videoRef.current;
      if (!video) return;

      const now = Date.now();
      if (!force && now - lastSavedAtRef.current < 10000) return;

      const position = Number.isFinite(video.currentTime) ? video.currentTime : 0;
      const duration = Number.isFinite(video.duration) ? video.duration : 0;
      if (position <= 0 && !force) return;

      lastSavedAtRef.current = now;
      const payload = JSON.stringify({
        item_id: itemId,
        title: manifest?.title || title,
        position_seconds: Math.max(0, position),
        duration_seconds: Math.max(0, duration),
      });

      try {
        if (useKeepalive) {
          void fetch('/api/playback/progress', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
            keepalive: true,
          });
          return;
        }
        await apiFetch('/api/playback/progress', {
          method: 'POST',
          body: payload,
        });
        if (force) {
          onProgressSaved();
        }
      } catch {
        // non-blocking progress persistence
      }
    },
    [itemId, manifest?.title, onProgressSaved, title],
  );

  const handleClose = useCallback(() => {
    void saveProgress(true);
    onClose();
  }, [onClose, saveProgress]);

  // Fetch or reuse manifest when itemId changes
  useEffect(() => {
    if (!itemId) {
      setManifest(null);
      setError(null);
      return;
    }

    let cancelled = false;
    setUsingDirectFallback(false);
    setAudioTracks([]);
    setActiveAudioTrack(0);

    if (prefetchedManifest && prefetchedManifest.item_id === itemId) {
      setManifest(prefetchedManifest);
      setLoading(false);
      setError(null);
      return;
    }

    setLoading(true);
    setError(null);
    setWaitingNote(null);
    setSilentAudio(false);
    const isTorrent = itemId.startsWith('qb:');
    const startedAt = Date.now();
    let retryTimer: number | undefined;

    // A torrent that was just grabbed has no file list yet (409/503): keep
    // asking for up to 90 s instead of failing straight away.
    const load = () => {
      apiFetch<StreamManifestResponse>(
        `/api/stream/${encodeURIComponent(itemId)}/manifest?title=${encodeURIComponent(title)}`,
      )
        .then((data) => {
          if (cancelled) return;
          setWaitingNote(null);
          setManifest(data);
          setLoading(false);
        })
        .catch((err) => {
          if (cancelled) return;
          const waiting = err instanceof ApiError && (err.status === 409 || err.status === 503);
          if (isTorrent && waiting && Date.now() - startedAt < 90_000) {
            setWaitingNote('Waiting for the torrent to start…');
            retryTimer = window.setTimeout(load, 3000);
            return;
          }
          setWaitingNote(null);
          setError(err instanceof ApiError ? err.message : 'Unable to load stream manifest.');
          setLoading(false);
        });
    };
    load();

    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
    };
  }, [itemId, prefetchedManifest, title]);

  // Attach HLS or direct stream to <video>
  useEffect(() => {
    if (!itemId || !manifest || !videoRef.current) return;
    const video = videoRef.current;
    let cancelled = false;

    const cleanupHls = () => {
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
    };

    const attachDirect = () => {
      cleanupHls();
      if (manifest.direct_url) {
        video.src = manifest.direct_url;
      } else {
        setError('No playable stream URL is available for this item.');
      }
    };

    const onLoadedMetadata = () => {
      const resume = Number(manifest.resume_seconds || 0);
      if (resume > 0 && Number.isFinite(video.duration) && resume < video.duration - 2) {
        video.currentTime = resume;
      }
    };

    video.addEventListener('loadedmetadata', onLoadedMetadata, { once: true });

    // Still downloading: when playback reaches a part that isn't there yet the
    // server answers 503 and the element errors. Reload from where we were.
    let lastGoodTime = 0;
    let retryTimer: number | undefined;
    const onTime = () => {
      if (video.currentTime > 0) lastGoodTime = video.currentTime;
    };
    const onDirectError = () => {
      if (!manifest.downloading || hlsRef.current) return;
      setWaitingNote('Waiting for the next part to download…');
      window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(() => {
        if (cancelled || !manifest.direct_url) return;
        const resumeAt = lastGoodTime;
        video.src = manifest.direct_url;
        video.addEventListener(
          'loadedmetadata',
          () => {
            if (resumeAt > 0) video.currentTime = resumeAt;
            void video.play().catch(() => {});
          },
          { once: true },
        );
      }, 4000);
    };
    const onPlaying = () => setWaitingNote(null);

    // Browsers decode the picture but not Dolby (AC-3/E-AC-3/TrueHD) or DTS
    // audio, so direct play of those files is silent. Chrome and Firefox expose
    // enough to notice; Safari plays E-AC-3 itself.
    let silenceTimer: number | undefined;
    const checkSilence = () => {
      if (hlsRef.current) return;
      const v = video as HTMLVideoElement & {
        webkitAudioDecodedByteCount?: number;
        webkitVideoDecodedByteCount?: number;
        mozHasAudio?: boolean;
      };
      const chromeSilent =
        typeof v.webkitAudioDecodedByteCount === 'number' &&
        (v.webkitVideoDecodedByteCount ?? 0) > 0 &&
        v.webkitAudioDecodedByteCount === 0;
      const firefoxSilent = v.mozHasAudio === false;
      setSilentAudio(Boolean(chromeSilent || firefoxSilent));
    };
    const onPlayingCheck = () => {
      window.clearTimeout(silenceTimer);
      silenceTimer = window.setTimeout(checkSilence, 4000);
    };
    video.addEventListener('timeupdate', onTime);
    video.addEventListener('error', onDirectError);
    video.addEventListener('playing', onPlaying);
    video.addEventListener('playing', onPlayingCheck);

    if (manifest.hls_url && manifest.hls_configured && !usingDirectFallback) {
      import('hls.js')
        .then(({ default: Hls }) => {
          if (cancelled) return;
          if (Hls.isSupported()) {
            const hls = new Hls({ enableWorker: true });
            hlsRef.current = hls;
            hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, () => {
              setAudioTracks([...(hls.audioTracks || [])]);
              setActiveAudioTrack(hls.audioTrack ?? 0);
            });
            hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, () => {
              setActiveAudioTrack(hls.audioTrack ?? 0);
            });
            hls.on(Hls.Events.ERROR, (_ev, data) => {
              if (data?.fatal) {
                setUsingDirectFallback(true);
                attachDirect();
              }
            });
            hls.loadSource(manifest.hls_url!);
            hls.attachMedia(video);
          } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = manifest.hls_url!;
          } else {
            attachDirect();
          }
        })
        .catch(() => {
          if (!cancelled) {
            setUsingDirectFallback(true);
            attachDirect();
          }
        });
    } else {
      attachDirect();
    }

    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(silenceTimer);
      video.removeEventListener('loadedmetadata', onLoadedMetadata);
      video.removeEventListener('timeupdate', onTime);
      video.removeEventListener('error', onDirectError);
      video.removeEventListener('playing', onPlaying);
      video.removeEventListener('playing', onPlayingCheck);
      cleanupHls();
    };
  }, [itemId, manifest, usingDirectFallback]);

  // Rebuild subtitle Blob URL whenever file or offset changes
  useEffect(() => {
    if (!subtitleRaw) {
      setSubtitleUrl(null);
      return;
    }
    const vttText = buildShiftedVtt(subtitleRaw, subtitleOffset);
    const blob = new Blob([vttText], { type: 'text/vtt' });
    const url = URL.createObjectURL(blob);
    setSubtitleUrl(url);
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [subtitleRaw, subtitleOffset]);

  // Save progress with keepalive on page unload
  useEffect(() => {
    if (!itemId) return;
    const onBeforeUnload = () => {
      void saveProgress(true, true);
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
    };
  }, [itemId, saveProgress]);

  // Keyboard shortcuts
  useEffect(() => {
    if (!itemId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        if (e.key === 'Escape') {
          handleClose();
        }
        return;
      }

      const video = videoRef.current;
      switch (e.key) {
        case 'Escape':
          e.preventDefault();
          handleClose();
          break;
        case ' ':
          e.preventDefault();
          if (video) {
            if (video.paused) void video.play();
            else video.pause();
          }
          break;
        case 'ArrowLeft':
          e.preventDefault();
          if (video) video.currentTime = Math.max(0, video.currentTime - 10);
          break;
        case 'ArrowRight':
          e.preventDefault();
          if (video) video.currentTime = Math.min(video.duration || Infinity, video.currentTime + 10);
          break;
        case 'j':
        case 'J':
          e.preventDefault();
          if (video) video.currentTime = Math.max(0, video.currentTime - 30);
          break;
        case 'l':
        case 'L':
          e.preventDefault();
          if (video) video.currentTime = Math.min(video.duration || Infinity, video.currentTime + 30);
          break;
        case 'm':
        case 'M':
          e.preventDefault();
          if (video) video.muted = !video.muted;
          break;
        case 'f':
        case 'F':
          e.preventDefault();
          if (document.fullscreenElement) {
            void document.exitFullscreen();
          } else {
            void containerRef.current?.requestFullscreen?.();
          }
          break;
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [handleClose, itemId]);

  const backdropRef = useRef<HTMLDivElement | null>(null);
  const backdropDismiss = useBackdropDismiss(handleClose);
  useDialogEnter(backdropRef, containerRef, Boolean(itemId));

  if (!itemId) return null;

  function handleSubtitleFile(file: File | undefined) {
    if (!file) return;
    setSubtitleLabel(file.name);
    setSubtitleOffset(0);
    file.text().then((text) => setSubtitleRaw(text));
  }

  return (
    <div
      ref={backdropRef}
      className="bp-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={`Theater Player: ${manifest?.title || title}`}
      onPointerDown={backdropDismiss.onPointerDown}
      onClick={backdropDismiss.onClick}
    >
      <div ref={containerRef} className="bp-modal bp-modal-wide">
        <div className="bp-row">
          <div>
            <p className="bp-kicker">4K theater</p>
            <h2 style={{ margin: '0.2rem 0 0', fontSize: '1.35rem' }}>{manifest?.title || title}</h2>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginTop: '0.45rem' }}>
              {manifest?.resolution ? <span className="bp-tag">{manifest.resolution}</span> : null}
              {manifest?.hdr_format ? <span className="bp-tag">{manifest.hdr_format}</span> : null}
              {usingDirectFallback ? <span className="bp-tag">Direct fallback</span> : null}
            </div>
          </div>
          <button type="button" className="bp-pill bp-pill-ghost" onClick={handleClose}>
            Close (Esc)
          </button>
        </div>

        {manifest?.dv_profile_notice ? (
          <div className="bp-alert" style={{ marginTop: '0.9rem' }}>
            Dolby Vision release detected. If colors look green/purple in browser playback, open in an external player
            (MPV / VLC / Infuse).
          </div>
        ) : null}

        {error ? (
          <div className="bp-alert" role="alert" style={{ marginTop: '0.9rem' }}>
            {error}
          </div>
        ) : null}

        {manifest?.downloading ? (
          <p className="bp-muted" role="status" style={{ marginTop: '0.9rem', fontSize: 14 }}>
            Still downloading ({manifest.download_progress ?? 0}% done,{' '}
            {manifest.playable_progress ?? 0}% playable from the start). It downloads in order, so playback
            pauses only if you catch up with it.
          </p>
        ) : null}

        {waitingNote ? (
          <p className="bp-muted" role="status" style={{ marginTop: '0.6rem', fontSize: 14 }}>
            {waitingNote}
          </p>
        ) : null}

        {silentAudio ? (
          <div className="bp-alert" role="alert" style={{ marginTop: '0.9rem' }}>
            No sound: this file's audio is probably Dolby or DTS, which browsers can't decode.{' '}
            {manifest?.hls_configured && usingDirectFallback ? (
              <button
                type="button"
                className="bp-pill bp-pill-ghost"
                style={{ marginLeft: '0.4rem' }}
                onClick={() => {
                  setSilentAudio(false);
                  setUsingDirectFallback(false);
                }}
              >
                Try Jellyfin's converted stream
              </button>
            ) : manifest?.vlc_url ? (
              <>Open it in VLC or Infuse below{manifest.downloading ? ', or wait until it finishes so Jellyfin can convert the audio' : ''}.</>
            ) : manifest?.downloading ? (
              <>Wait until it finishes downloading so Jellyfin can convert the audio.</>
            ) : null}
          </div>
        ) : null}

        {loading ? (
          <p className="bp-muted" style={{ marginTop: '1rem' }}>
            Preparing stream…
          </p>
        ) : null}

        <div style={{ marginTop: '1rem', borderRadius: 16, overflow: 'hidden', background: '#000' }}>
          <video
            ref={videoRef}
            controls
            playsInline
            style={{ width: '100%', maxHeight: '62vh', display: 'block' }}
            onTimeUpdate={() => void saveProgress(false)}
            onPause={() => void saveProgress(true)}
            onEnded={() => void saveProgress(true)}
          >
            {subtitleUrl ? (
              <track kind="subtitles" src={subtitleUrl} label={subtitleLabel || 'Local Subtitles'} default />
            ) : null}
          </video>
        </div>

        <div className="bp-row" style={{ marginTop: '1rem' }}>
          <div className="bp-nav-actions">
            {audioTracks.length > 1 ? (
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4rem', fontSize: 13 }}>
                <span className="bp-kicker">Audio</span>
                <select
                  className="bp-input"
                  style={{ height: 38, width: 'auto', fontSize: 13, padding: '0 0.9rem' }}
                  value={activeAudioTrack}
                  onChange={(e) => {
                    const idx = Number(e.target.value);
                    if (hlsRef.current) {
                      hlsRef.current.audioTrack = idx;
                      setActiveAudioTrack(idx);
                    }
                  }}
                >
                  {audioTracks.map((trk, idx) => (
                    <option key={trk.id ?? idx} value={idx}>
                      {trk.name || trk.lang || `Track ${idx + 1}`}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            <label className="bp-pill bp-pill-ghost" style={{ cursor: 'pointer' }}>
              {subtitleLabel ? `Sub: ${subtitleLabel}` : 'Load .vtt / .srt'}
              <input
                type="file"
                accept=".vtt,.srt"
                className="sr-only"
                onChange={(e) => handleSubtitleFile(e.target.files?.[0])}
              />
            </label>

            {subtitleRaw ? (
              <>
                <button
                  type="button"
                  className="bp-pill bp-pill-ghost"
                  onClick={() => setSubtitleOffset((prev) => Number((prev - 0.5).toFixed(1)))}
                >
                  -0.5s
                </button>
                <span className="bp-tag">{subtitleOffset >= 0 ? `+${subtitleOffset}s` : `${subtitleOffset}s`}</span>
                <button
                  type="button"
                  className="bp-pill bp-pill-ghost"
                  onClick={() => setSubtitleOffset((prev) => Number((prev + 0.5).toFixed(1)))}
                >
                  +0.5s
                </button>
              </>
            ) : null}
          </div>

          <div className="bp-nav-actions">
            {manifest?.vlc_url ? (
              <a href={manifest.vlc_url} className="bp-pill bp-pill-ghost">
                VLC
              </a>
            ) : null}
            {manifest?.mpv_url ? (
              <a href={manifest.mpv_url} className="bp-pill bp-pill-ghost">
                MPV
              </a>
            ) : null}
            {manifest?.infuse_url ? (
              <a href={manifest.infuse_url} className="bp-pill bp-pill-ghost">
                Infuse
              </a>
            ) : null}
          </div>
        </div>

        <p className="bp-muted" style={{ margin: '0.75rem 0 0', fontVariantNumeric: 'tabular-nums', fontSize: 13 }}>
          Shortcuts: Space Play/Pause · ←/→ ±10s · J/L ±30s · M Mute · F Fullscreen · Esc Close
        </p>
      </div>
    </div>
  );
}
