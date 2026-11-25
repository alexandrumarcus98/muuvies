from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


# Auth & Vault
class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    invite_code: Optional[str] = None


class AuthConfigResponse(BaseModel):
    allow_registration: bool
    invite_required: bool
    vault_timeout_minutes: int
    require_email_verification: bool = False


class VerificationSentResponse(BaseModel):
    status: str = "verification_sent"
    email: str
    message: str


class VerifyEmailRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)


class ResendVerificationRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)
    new_password: str = Field(..., min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


class LoginRequest(BaseModel):
    username: str  # Accepts username or email
    password: str


class LoginResponse(BaseModel):
    success: bool
    username: str
    email: Optional[str] = None
    role: str = "user"
    vault_unlocked: bool
    message: str


class VaultUnlockRequest(BaseModel):
    pin: str


class VaultStatusResponse(BaseModel):
    unlocked: bool
    expires_in_seconds: int = 0
    message: str


class PasskeyOptionsResponse(BaseModel):
    challenge_id: str
    options: Dict[str, Any]


class PasskeyRegisterVerifyRequest(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=256)
    credential: Dict[str, Any]
    device_name: str = Field(default="Passkey", max_length=128)


class PasskeyAuthVerifyRequest(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=256)
    credential: Dict[str, Any]


class PasskeyInfo(BaseModel):
    id: int
    device_name: str
    transports: List[str] = Field(default_factory=list)
    sign_count: int = 0
    created_at: datetime
    last_used_at: Optional[datetime] = None


class PasskeyListResponse(BaseModel):
    passkeys: List[PasskeyInfo] = Field(default_factory=list)


# Search & AI
class SearchRequest(BaseModel):
    query: str
    category: str = "All"  # All, Movies, TV Shows, Adult
    ai_smart_search: bool = False
    deep: bool = False
    limit: int = 100
    page: int = 1
    page_size: int = 20
    sort_by: str = "smart"  # smart, seeders, size_desc, size_asc, newest


class QualityBadge(BaseModel):
    label: str
    color: str = "blue"  # green, blue, yellow, purple, gray


class SearchResultItem(BaseModel):
    guid: str
    title: str
    category: str
    indexer: str
    sources: List[str] = Field(default_factory=list)
    best_source: Optional[str] = None
    size: int
    size_formatted: str
    seeders: int
    leechers: int
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    info_hash: Optional[str] = None
    publish_date: Optional[str] = None
    already_grabbed: bool = False
    can_grab: bool = True
    in_library: bool = False
    library_item_title: Optional[str] = None
    library_item_id: Optional[str] = None
    quality_score: Optional[int] = None
    muuvies_score: Optional[float] = None
    badges: List[QualityBadge] = Field(default_factory=list)
    ai_pick: bool = False
    ai_analysis: Optional[str] = None
    preview_token: Optional[str] = None


class AdultPreviewImage(BaseModel):
    id: str
    role: str


class AdultPreviewResponse(BaseModel):
    images: List[AdultPreviewImage] = Field(default_factory=list)
    source: str


class SmartRankingSummary(BaseModel):
    ai_active: bool
    query_intent: Optional[str] = None
    recommended_guid: Optional[str] = None
    recommended_title: Optional[str] = None
    recommendation_reason: Optional[str] = None
    model_used: Optional[str] = None


class CuratorCandidateInput(BaseModel):
    guid: str
    title: str
    size: int = 0
    size_formatted: str = ""
    seeders: int = 0
    indexer: str = "Unknown"
    sources: List[str] = Field(default_factory=list)
    quality_score: Optional[int] = None
    muuvies_score: Optional[float] = None
    badges: List[QualityBadge] = Field(default_factory=list)
    in_library: bool = False


class CuratorRequest(BaseModel):
    query: str
    category: str = "All"
    candidates: List[CuratorCandidateInput] = Field(default_factory=list)


class CuratorResponse(BaseModel):
    ai_ranking: Optional[SmartRankingSummary] = None
    ratings: Dict[str, str] = Field(default_factory=dict)
    cached: bool = False


class SearchResponse(BaseModel):
    query: str
    category: str
    total_results: int
    page: int = 1
    page_size: int = 20
    total_pages: int = 1
    total_found: int = 0
    results: List[SearchResultItem]
    ai_ranking: Optional[SmartRankingSummary] = None
    vault_active: bool = False  # True if stealth mode active
    deep_active: bool = False


class SearchSuggestionItem(BaseModel):
    title: str
    category: str = "Media"  # Movie, TV Series, History
    year: Optional[int] = None
    source: str = "library"  # library, history, trending


class SearchSuggestionsResponse(BaseModel):
    query: str
    suggestions: List[SearchSuggestionItem]


# Grabbing
class QuickGrabRequest(BaseModel):
    title: str
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    info_hash: Optional[str] = None
    category: str = "Movies"  # Movies, TV Shows, adult
    indexer: str = "Unknown"
    size: int = 0
    # Watch while downloading: sequential download + first/last pieces first.
    stream: bool = False


class ArrMonitorRequest(BaseModel):
    title: str
    category: str = "Movies"  # Movies, TV Shows, adult
    indexer: str = "Unknown"
    size: int = 0
    guid: Optional[str] = None
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    info_hash: Optional[str] = None
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    tvdb_id: Optional[int] = None


class GrabResponse(BaseModel):
    success: bool
    target: str  # qbittorrent, radarr, sonarr, whisparr
    message: str
    info_hash: Optional[str] = None
    history_id: Optional[int] = None


class GrabHistoryItem(BaseModel):
    id: int
    title: str
    category: str
    indexer: str
    size: int
    size_formatted: str
    target: str
    status: str
    info_hash: Optional[str] = None
    created_at: datetime


# Queue
class TorrentQueueItem(BaseModel):
    hash: str
    name: str
    size: int
    size_formatted: str
    progress: float  # 0.0 - 100.0
    download_speed: int
    upload_speed: int
    download_speed_formatted: str
    upload_speed_formatted: str
    eta_seconds: int
    eta_formatted: str
    state: str
    category: str
    seeds: int
    peers: int
    # Sequential + first/last-piece priority on: can be watched while downloading.
    streaming: bool = False


class QueueActionRequest(BaseModel):
    hashes: List[str]
    action: str  # pause, resume, delete
    delete_files: bool = False


# Telemetry
class ServiceStatus(BaseModel):
    name: str
    connected: bool
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    version: Optional[str] = None
    url: str
    launcher_url: str
    protected_by_vault: bool = False


class TelemetryResponse(BaseModel):
    services: List[ServiceStatus]
    download_speed_bps: int
    upload_speed_bps: int
    download_speed_formatted: str
    upload_speed_formatted: str
    free_disk_space_bytes: int
    free_disk_space_formatted: str
    # Admin-only infra details; empty strings for everyone else.
    seedbox_host: str = ""
    jellyfin_web_url: str = ""
    vault_unlocked: bool
    active_torrents_count: int


# Settings
class SettingsUpdateRequest(BaseModel):
    gemini_api_key: Optional[str] = None
    github_token: Optional[str] = None
    ai_provider: Optional[str] = None
    prowlarr_api_key: Optional[str] = None
    radarr_api_key: Optional[str] = None
    sonarr_api_key: Optional[str] = None
    whisparr_api_key: Optional[str] = None
    cross_seed_api_key: Optional[str] = None
    qbittorrent_password: Optional[str] = None
    vault_pin: Optional[str] = None
    sentry_dsn: Optional[str] = None


class SettingsResponse(BaseModel):
    ai_provider: str
    gemini_api_key_configured: bool
    gemini_model: str
    github_token_configured: bool
    github_model: str
    seedbox_host: str
    seedbox_user: str
    qbittorrent_configured: bool
    prowlarr_configured: bool
    radarr_configured: bool
    sonarr_configured: bool
    whisparr_configured: bool
    cross_seed_configured: bool
    vault_pin_configured: bool
    sentry_configured: bool = False


# Playback Progress
class PlaybackProgressUpdate(BaseModel):
    item_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)
    position_seconds: float = Field(..., ge=0.0)
    duration_seconds: float = Field(default=0.0, ge=0.0)


class PlaybackProgressResponse(BaseModel):
    item_id: str
    title: str = ""
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    progress_pct: float = 0.0
    completed: bool = False
    updated_at: Optional[datetime] = None

