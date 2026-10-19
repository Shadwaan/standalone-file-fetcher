"""
Spotify playlist discovery and track listing.

Authenticates via PKCE — no Client Secret required, designed for desktop apps
that can't keep secrets. The Client ID below is sff's shared dev-app ID;
it's safe to ship publicly (Spotify designed Client IDs to be public).

End users do NOT need to create their own Spotify dev app. They just click
Sync and authorize sff in their browser. Sff's owner (whoever publishes this
copy) does need to add each user's Spotify email to the app's Development-Mode
allowlist — see app/SETUP.md.

Power users can override the Client ID by setting SPOTIFY_CLIENT_ID in app/.env.
"""

import logging
import os
import re
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyPKCE

from models.track import TrackInfo

logger = logging.getLogger(__name__)

# sff's shared Spotify dev-app Client ID. Public by design (sent in every
# OAuth URL). Override via SPOTIFY_CLIENT_ID env var if you have your own app.
DEFAULT_CLIENT_ID = "ee8d13f0effb403ca47b7fe518b55633"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9]{22}$")
NOT_READABLE_MSG = ("Spotify only lets sff read the tracks of playlists you own or are a collaborator on, and this one "
                    "belongs to somebody else. Copy its tracks into one of your own playlists instead (or ask the owner to make "
                    "it collaborative and join it).")


def parse_playlist_ref(text: str) -> str | None:
    """The Spotify playlist id in a link (https://open.spotify.com/playlist/<id>?si=...), a spotify:playlist:<id>
    URI, or a bare id. None when the text is none of those (it is then taken to be a playlist NAME)."""
    text = (text or "").strip()
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?playlist/([A-Za-z0-9]{22})", text) \
        or re.search(r"spotify:playlist:([A-Za-z0-9]{22})", text)
    if m:
        return m.group(1)
    return text if _PLAYLIST_ID.match(text) else None


class SpotifyService:
    """Spotify API client for playlist discovery and track listing."""

    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID") or DEFAULT_CLIENT_ID
        self.redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        self.prefix = os.getenv("PLAYLIST_PREFIX", "FF")
        self.sp = self._authenticate()

    def _authenticate(self) -> spotipy.Spotify:
        """Authenticate with Spotify using PKCE (no Client Secret needed)."""
        cache_path = Path(__file__).parent.parent / ".spotify_cache"
        auth_manager = SpotifyPKCE(
            client_id=self.client_id,
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

    def get_playlist_info(self, playlist_id: str) -> dict:
        """Name, owner and snapshot of any playlist you can read (your own, or somebody else's you follow or that is public)."""
        info = self.sp.playlist(playlist_id, fields="id,name,snapshot_id,owner(display_name)")
        return {"id": info["id"], "name": info["name"], "snapshot_id": info.get("snapshot_id", ""),
                "owner": ((info.get("owner") or {}).get("display_name") or ""), "track_count": 0}

    def can_read_tracks(self, playlist_id: str) -> bool:
        """Whether Spotify will hand over this playlist's TRACKS. Its name and owner can be looked up for any public
        playlist, but the tracks of somebody else's playlist are refused to an app like sff (403)."""
        try:
            self.sp.playlist_items(playlist_id, limit=1, additional_types=["track"])
            return True
        except Exception as e:
            logger.info("tracks of playlist %s are not readable: %s", playlist_id, str(e).splitlines()[0][:120])
            return False

    def find_library_playlist(self, name: str) -> dict | None:
        """A playlist in your Spotify library (made by you OR by somebody else and saved) with exactly this name."""
        wanted = name.strip().lower()
        results = self.sp.current_user_playlists(limit=50)
        while results:
            for item in results.get("items", []):
                if item and (item.get("name") or "").strip().lower() == wanted:
                    return {"id": item["id"], "name": item["name"], "snapshot_id": item.get("snapshot_id", ""),
                            "owner": ((item.get("owner") or {}).get("display_name") or ""), "track_count": 0}
            results = self.sp.next(results) if results.get("next") else None
        return None

    def resolve_playlist(self, ref: str) -> dict:
        """A playlist from a link/URI/id, or else from its exact name in your library. Raises ValueError when not found."""
        pid = parse_playlist_ref(ref)
        try:
            if pid:
                return self.get_playlist_info(pid)
            found = self.find_library_playlist(ref)
        except Exception as e:
            raise ValueError(f"Spotify would not give me that playlist ({e})") from e
        if not found:
            raise ValueError(f"No playlist named '{ref.strip()}' in your Spotify library. Paste its link instead.")
        return found

    def resolve_followable(self, ref: str) -> dict:
        """resolve_playlist, but only for a playlist whose tracks can really be read: refusing here is kinder than
        following something that fails on every sync."""
        info = self.resolve_playlist(ref)
        if not self.can_read_tracks(info["id"]):
            who = f" ({info['owner']})" if info.get("owner") else ""
            raise ValueError(f"'{info['name']}'{who}: {NOT_READABLE_MSG}")
        return info

    def get_all_playlists(self) -> list[dict]:
        """The prefixed playlists plus every followed playlist from other people. A followed playlist Spotify will not
        hand over is left out and listed in `self.skipped` (name, reason), so one bad link never stops a sync."""
        from services import app_config
        playlists = self.get_prefixed_playlists()
        seen = {p["id"] for p in playlists}
        self.skipped = []
        for extra in app_config.get_extra_playlists():
            if extra["id"] in seen:
                continue
            try:
                info = self.get_playlist_info(extra["id"])
                if not self.can_read_tracks(extra["id"]):
                    raise ValueError(NOT_READABLE_MSG)
            except Exception as e:
                logger.warning("Could not read followed playlist %s (%s): %s", extra.get("name"), extra["id"], e)
                self.skipped.append((extra.get("display_name") or extra.get("name") or extra["id"], str(e)))
                continue
            playlists.append({"id": info["id"], "name": info["name"],
                              "display_name": extra.get("display_name") or info["name"],
                              "snapshot_id": info["snapshot_id"], "track_count": 0})
            seen.add(info["id"])
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
