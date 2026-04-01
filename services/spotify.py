"""
Spotify playlist discovery and track listing.

Connects via spotipy OAuth for private playlist access.
Discovers all playlists with the configured prefix (default "FF").
"""

import logging
import os
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from models.track import TrackInfo

logger = logging.getLogger(__name__)


class SpotifyService:
    """Spotify API client for playlist discovery and track listing."""

    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
        self.redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
        self.prefix = os.getenv("PLAYLIST_PREFIX", "FF")
        self.sp = self._authenticate()

    def _authenticate(self) -> spotipy.Spotify:
        """Authenticate with Spotify using OAuth for private playlist access."""
        cache_path = Path(__file__).parent.parent / ".spotify_cache"
        auth_manager = SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope="playlist-read-private playlist-read-collaborative",
            cache_path=str(cache_path),
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)
        try:
            user = sp.current_user()
            logger.info("Spotify authenticated as: %s", user.get("display_name", "unknown"))
        except Exception as e:
            logger.error("Spotify authentication failed: %s", e)
            raise
        return sp

    def get_prefixed_playlists(self) -> list[dict]:
        """Find all user playlists with the configured prefix."""
        playlists = []
        results = self.sp.current_user_playlists(limit=50)

        while results:
            for item in results.get("items", []):
                if not item or not item.get("name"):
                    continue
                if item["name"].startswith(self.prefix):
                    tracks_info = item.get("tracks") or {}
                    playlists.append({
                        "id": item["id"],
                        "name": item["name"],
                        "display_name": item["name"][len(self.prefix):].strip(),
                        "snapshot_id": item.get("snapshot_id", ""),
                        "track_count": tracks_info.get("total", 0),
                    })
            if results.get("next"):
                results = self.sp.next(results)
            else:
                break

        logger.info("Found %d prefixed playlists (prefix='%s')", len(playlists), self.prefix)
        return playlists

    def get_playlist_tracks(self, playlist_id: str, playlist_name: str) -> list[TrackInfo]:
        """Get all tracks from a playlist with full metadata."""
        tracks = []
        # Don't use fields parameter — it filters out the item field
        results = self.sp.playlist_items(
            playlist_id,
            additional_types=["track"],
        )

        position = 0
        while results:
            for item in results["items"]:
                # API returns item field (NOT track)
                track = item.get("track") or item.get("item")
                if not track or not track.get("id"):
                    continue

                artists = ", ".join(a["name"] for a in track["artists"])
                album = track.get("album", {})
                images = album.get("images", [])
                artwork_url = images[0]["url"] if images else None
                release_date = album.get("release_date", "")
                year = release_date[:4] if release_date else ""

                tracks.append(TrackInfo(
                    spotify_id=track["id"],
                    title=track["name"],
                    artist=artists,
                    album=album.get("name", ""),
                    year=year,
                    duration_ms=track["duration_ms"],
                    artwork_url=artwork_url,
                    playlist_name=playlist_name,
                    position=position,
                ))
                position += 1

            if results.get("next"):
                results = self.sp.next(results)
            else:
                break

        logger.info("Playlist '%s': %d tracks", playlist_name, len(tracks))
        return tracks
