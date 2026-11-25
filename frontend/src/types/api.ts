export type ThemeMode = 'dark' | 'light';
export type WorkspaceTab = 'search' | 'downloads' | 'maintenance' | 'admin' | 'settings';

export interface AdminUserItem {
  id: number;
  username: string;
  email: string;
  role: 'admin' | 'user' | string;
  is_active: boolean;
  email_verified: boolean;
  passkey_count: number;
  token_version: number;
  created_at: string;
}

export interface AdminUsersResponse {
  users: AdminUserItem[];
}

export interface AdminUserUpdateRequest {
  role?: 'admin' | 'user';
  is_active?: boolean;
  revoke_sessions?: boolean;
}

export interface AuthMeResponse {
  authenticated: boolean;
  username?: string;
  email?: string;
  role?: 'user' | 'admin' | string;
  vault_unlocked?: boolean;
}

export interface AuthConfigResponse {
  allow_registration: boolean;
  invite_required: boolean;
  vault_timeout_minutes: number;
}

export interface AuthActionResponse {
  status?: string;
  username?: string;
  email?: string;
  role?: string;
  vault_unlocked?: boolean;
  message?: string;
  detail?: string;
  code?: string;
}

export interface PasskeyItem {
  id: number;
  device_name: string;
  created_at?: string;
  last_used_at?: string | null;
}

export interface SuggestionItem {
  title: string;
  year?: string | number;
  res?: string;
  category?: string;
  source?: string;
}

export interface QualityBadge {
  label: string;
  color?: string;
}

export interface SearchResultItem {
  is_adult?: boolean;
  guid: string;
  title: string;
  size?: number;
  size_formatted?: string;
  seeders?: number;
  indexer?: string;
  category?: string;
  sources?: string[];
  muuvies_score?: number;
  quality_score?: number;
  /** QualityBadge from the backend ({label, color}); plain strings tolerated. */
  badges?: Array<QualityBadge | string>;
  in_library?: boolean;
  library_item_id?: string | number | null;
  already_grabbed?: boolean;
  can_grab?: boolean;
  download_url?: string | null;
  magnet_url?: string | null;
  info_hash?: string | null;
  preview_token?: string | null;
  res?: string;
}

export interface AiRankingInfo {
  recommended_guid?: string | null;
  recommendation_reason?: string | null;
}

export interface SearchResponse {
  results: SearchResultItem[];
  page: number;
  total_pages: number;
  total_found: number;
  query: string;
  category: string;
  ai_ranking?: AiRankingInfo | null;
}

export interface PreviewImageItem {
  id: string;
  width?: number;
  height?: number;
}

export interface PreviewResponse {
  images: PreviewImageItem[];
  source?: string;
}

export interface QueueTorrentItem {
  hash: string;
  name: string;
  state: string;
  progress: number;
  size_formatted?: string;
  dlspeed_formatted?: string;
  upspeed_formatted?: string;
  eta_formatted?: string;
  ratio?: number;
  category?: string;
  /** Sequential download + first/last pieces first: watchable while downloading. */
  streaming?: boolean;
}

export interface ServiceHealthItem {
  name: string;
  connected: boolean;
  detail?: string;
}

export interface TelemetryResponse {
  services: ServiceHealthItem[];
  download_speed_formatted: string;
  upload_speed_formatted: string;
  free_disk_space_formatted: string;
  seedbox_host?: string;
  jellyfin_web_url?: string | null;
  active_torrents_count: number;
  vault_unlocked: boolean;
}

export interface LibraryItem {
  id: string | number;
  title: string;
  year?: number | string | null;
  type?: string;
  resolution?: string | null;
  poster_url?: string | null;
}

export interface LibraryResponse {
  movies: LibraryItem[];
  shows: LibraryItem[];
  adult: LibraryItem[];
  total_movies: number;
  total_shows: number;
  total_adult: number;
}

export interface GrabHistoryItem {
  id?: number;
  title: string;
  category?: string;
  indexer?: string;
  target?: string;
  created_at?: string;
}

export interface StreamManifestResponse {
  item_id: string;
  title: string;
  resolution?: string | null;
  hdr_format?: string | null;
  dv_profile_notice?: boolean;
  hls_url?: string | null;
  hls_configured?: boolean;
  direct_url?: string | null;
  vlc_url?: string | null;
  mpv_url?: string | null;
  infuse_url?: string | null;
  mpv_command?: string | null;
  resume_seconds?: number;
  is_adult?: boolean;
  /** A qBittorrent torrent that hasn't finished: direct play of the downloaded part only. */
  downloading?: boolean;
  streaming_mode?: boolean;
  download_progress?: number | null;
  /** Percent of the file playable from the start without a gap. */
  playable_progress?: number | null;
}

export interface AdminUserItem {
  id: number;
  username: string;
  email: string;
  role: 'admin' | 'user' | string;
  is_active: boolean;
  email_verified: boolean;
  passkey_count: number;
  token_version: number;
  created_at: string;
}

export interface AdminUsersResponse {
  users: AdminUserItem[];
}

export interface PlaybackProgressItem {
  item_id: string;
  title: string;
  position_seconds: number;
  duration_seconds: number;
  completed?: boolean;
  progress_pct?: number;
}

export interface MaintenanceReport {
  disk_free_formatted?: string;
  low_disk_space?: boolean;
  stalled_torrents?: Array<{ hash: string; name: string; state?: string; age_hours?: number }>;
  arr_warnings?: Array<{ service: string; queue_id?: number; title: string; message?: string; media_id?: number; episode_ids?: number[] }>;
  indexer_issues?: Array<{ name: string; message?: string }>;
  duplicates?: Array<{ title: string; count: number }>;
  heavy_files?: Array<{ name: string; size_formatted?: string }>;
  reclaimable_gb?: number;
  seeding_locked_gb?: number;
  issue_count: number;
}

export interface VaultStatusResponse {
  unlocked: boolean;
  expires_in_seconds: number;
}
