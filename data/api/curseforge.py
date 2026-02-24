"""
CurseForge API provider implementation.

Uses the ``curseforge`` library (https://pypi.org/project/curseforge/) for all
CurseForge API interactions.  The library handles authentication headers and
provides a reliable download-URL fallback for files whose ``downloadUrl`` field
is ``null`` in the API response (a common source of 403/download errors).
"""

import logging
import os
import requests
import time
from typing import Dict, Optional, Any
from pathlib import Path

from tqdm import tqdm
from curseforge.base import CurseClient

from data.api.base import BaseProvider

# CurseForge game ID for Minecraft
CURSEFORGE_GAME_ID = 432

# Built-in public API key shared by several open-source Minecraft launchers
# (e.g. Prism Launcher).  This allows CurseForge to work out of the box
# without requiring users to supply their own key.  Users can override it by
# setting the CURSEFORGE_API_KEY environment variable or via the config file.
# Obtain your own key at: https://console.curseforge.com/
CURSEFORGE_DEFAULT_API_KEY = "$2a$10$bL4bIL5pUWqfcO7KwT2NleecEFV7SqMGqaGFRZLfRMOSMTWrIGaSq"


class CurseForgeProvider(BaseProvider):
    """Provider for interacting with the CurseForge API via the curseforge library."""

    def __init__(self, api_key: str = ""):
        """
        Initialize the CurseForge provider.

        Key resolution order (first non-empty value wins):
        1. ``api_key`` argument (from config file)
        2. ``CURSEFORGE_API_KEY`` environment variable
        3. Built-in default key

        Args:
            api_key: Optional personal CurseForge API key.
                     Obtain one at https://console.curseforge.com/
        """
        self.logger = logging.getLogger(__name__)

        # Resolve the key to use, preferring explicit > env-var > built-in
        env_key = os.environ.get("CURSEFORGE_API_KEY", "").strip()
        api_key = api_key.strip() if api_key else ""
        if api_key:
            self.api_key = api_key
            self.logger.debug("Using CurseForge API key from configuration.")
        elif env_key:
            self.api_key = env_key
            self.logger.debug("Using CurseForge API key from CURSEFORGE_API_KEY environment variable.")
        else:
            self.api_key = CURSEFORGE_DEFAULT_API_KEY
            self.logger.debug("No custom CurseForge API key provided; using built-in default.")

        # CurseClient handles auth headers and SSL transparently
        self._client = CurseClient(api_key=self.api_key)
        self.max_retries = 3
        self.retry_delay = 1  # seconds

    def get_project_id(self, mod_id: str) -> Optional[str]:
        """
        Get the CurseForge project ID for a mod.

        Args:
            mod_id: The mod ID (slug or name) to look up

        Returns:
            CurseForge project ID or None if not found
        """
        try:
            params = {
                "gameId": CURSEFORGE_GAME_ID,
                "searchFilter": mod_id,
                "classId": 6,  # Mod class ID
                "pageSize": 5
            }

            results = self._fetch_with_retry("mods/search", params=params)
            if results is None:
                return None

            # Try to find an exact match first
            for result in results:
                if result.get('slug') == mod_id or result.get('name', '').lower() == mod_id.lower():
                    self.logger.info(f"Found exact match for mod {mod_id} on CurseForge: {result.get('id')}")
                    return str(result.get('id'))

            # If no exact match, use the first result if available
            if results:
                self.logger.info(f"Using best match for mod {mod_id} on CurseForge: {results[0].get('id')}")
                return str(results[0].get('id'))

            self.logger.warning(f"No results found for mod {mod_id} on CurseForge")
            return None
        except Exception as e:
            self.logger.error(f"Error searching for mod {mod_id} on CurseForge: {str(e)}")
            return None

    def get_latest_version(
        self,
        project_id: str,
        game_version: str,
        mod_loader: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get the latest version of a mod from CurseForge.

        Args:
            project_id: CurseForge project ID
            game_version: Minecraft game version to filter by
            mod_loader: Mod loader to filter by (fabric, forge, quilt, neoforge)

        Returns:
            Dictionary containing version info or None if not found
        """
        try:
            params = {
                "gameVersion": game_version,
                "modLoaderType": self._map_mod_loader_to_curseforge(mod_loader),
                "pageSize": 20
            }

            files = self._fetch_with_retry(f"mods/{project_id}/files", params=params)
            if files is None:
                return None

            if not files:
                self.logger.warning(f"No files found for CurseForge project {project_id}")
                return None

            # Filter for files compatible with our game version
            filtered_files = [
                f for f in files
                if game_version in f.get('gameVersions', [])
                and f.get('isAvailable', True)
                and not f.get('isServerPack', False)
            ]

            if not filtered_files:
                self.logger.warning(
                    f"No compatible files found for CurseForge project {project_id} "
                    f"with Minecraft {game_version} and {mod_loader}"
                )
                return None

            # Sort by fileDate descending to get the latest
            filtered_files.sort(key=lambda x: x.get('fileDate', ''), reverse=True)
            latest_file = filtered_files[0]

            download_url = latest_file.get('downloadUrl')
            file_id = latest_file.get('id')
            file_id_int = int(file_id) if file_id is not None else None

            # If the API returns no download URL, use the library's URL-guessing
            # fallback so we never hand back a bare "curseforge:<id>" placeholder.
            if not download_url:
                download_url = self._guess_download_url(file_id_int, latest_file.get('fileName', ''))

            return {
                'version_number': latest_file.get('displayName', '').split('-')[-1].strip(),
                'version_id': str(file_id),
                'date_published': latest_file.get('fileDate'),
                'game_versions': latest_file.get('gameVersions', []),
                'project_id': project_id,
                'files': [{'url': download_url or f"curseforge:{file_id}"}],
                'provider': 'curseforge'
            }
        except Exception as e:
            self.logger.error(f"Error getting versions for CurseForge project {project_id}: {str(e)}")
            return None

    def download_mod(
        self,
        version_info: Dict[str, Any],
        output_path: str
    ) -> bool:
        """
        Download a mod version to the specified path.

        Args:
            version_info: Version information dictionary from get_latest_version
            output_path: Path where the file should be saved

        Returns:
            True if download was successful, False otherwise
        """
        try:
            files = version_info.get('files', [])
            if not files:
                self.logger.warning("No download files available in version info")
                return False

            download_url = files[0].get('url')
            if not download_url:
                self.logger.warning("No download URL available in version info")
                return False

            # Handle residual curseforge: placeholder URLs
            if download_url.startswith("curseforge:"):
                file_id = download_url.replace("curseforge:", "")
                mod_id = version_info.get('project_id', '')
                resolved = self._get_direct_download_url(mod_id, file_id)
                if not resolved:
                    self.logger.error(f"Failed to get direct download URL for file {file_id}")
                    return False
                download_url = resolved

            # Stream the file to disk
            response = self._download_request(download_url)
            if not response:
                return False

            total_size = int(response.headers.get('content-length', 0))
            output_dir = Path(output_path).parent
            output_dir.mkdir(parents=True, exist_ok=True)

            with open(output_path, 'wb') as f:
                with tqdm(
                    total=total_size,
                    unit='B',
                    unit_scale=True,
                    desc=Path(output_path).name,
                    leave=False
                ) as pbar:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))

            self.logger.info(f"Successfully downloaded mod to {output_path}")
            return True
        except Exception as e:
            self.logger.error(f"Error downloading mod: {str(e)}")
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_with_retry(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:
        """
        Call ``CurseClient.fetch()`` with exponential-backoff retry logic.

        Args:
            endpoint: API endpoint path (e.g. ``"mods/search"``)
            params: Optional query parameters

        Returns:
            Parsed ``data`` payload (list or dict) or None on failure
        """
        for attempt in range(self.max_retries):
            try:
                result = self._client.fetch(endpoint, params=params or {})
                return result
            except Exception as e:
                self.logger.warning(
                    f"CurseForge request failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                )
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)
                    self.logger.info(f"Waiting {wait_time}s before retrying...")
                    time.sleep(wait_time)
                else:
                    self.logger.error(f"All {self.max_retries} attempts failed for endpoint '{endpoint}'")
        return None

    def _get_direct_download_url(self, mod_id: str, file_id: str) -> Optional[str]:
        """
        Resolve a direct download URL for a CurseForge file.

        Uses ``CurseClient.get_mod_file()`` which automatically constructs a
        CDN URL when the API returns ``null`` for ``downloadUrl``.

        Args:
            mod_id: CurseForge project/mod ID
            file_id: CurseForge file ID

        Returns:
            Direct download URL or None if unavailable
        """
        try:
            mod_file = self._client.get_mod_file(int(mod_id), int(file_id))
            return mod_file.download_url
        except Exception as e:
            self.logger.error(f"Error getting direct download URL for file {file_id}: {e}")
            return None

    @staticmethod
    def _guess_download_url(file_id: Optional[int], file_name: str) -> Optional[str]:
        """
        Construct the CDN download URL from a file ID and file name.

        Mirrors the logic used internally by the ``curseforge`` library when
        the API returns a ``null`` ``downloadUrl``.

        Args:
            file_id: CurseForge numeric file ID (must be >= 7 digits for a valid URL)
            file_name: Original file name (e.g. ``"mymod-1.0.jar"``)

        Returns:
            Constructed CDN URL or None if inputs are insufficient
        """
        if not file_id or not file_name:
            return None
        fid = str(file_id)
        if len(fid) < 7:
            return None
        part1 = fid[:4]
        part2 = fid[4:7]
        return f"https://edge.forgecdn.net/files/{part1}/{part2}/{file_name}"

    def _download_request(self, url: str) -> Optional[requests.Response]:
        """
        Stream a download URL with retry logic, using plain requests (no API key).

        Args:
            url: Full download URL

        Returns:
            Streaming Response object or None on failure
        """
        for attempt in range(self.max_retries):
            try:
                response = requests.get(url, stream=True, timeout=60)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                self.logger.warning(
                    f"Download request failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                )
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)
                    self.logger.info(f"Waiting {wait_time}s before retrying...")
                    time.sleep(wait_time)
                else:
                    self.logger.error(f"All {self.max_retries} download attempts failed: {e}")
        return None

    @staticmethod
    def _map_mod_loader_to_curseforge(mod_loader: str) -> int:
        """
        Map a mod loader string to its CurseForge numeric type ID.

        Args:
            mod_loader: Loader name (fabric, forge, quilt, neoforge)

        Returns:
            CurseForge mod loader type ID (0 = Any)
        """
        # CurseForge mod loader type IDs:
        # 1: Forge, 4: Fabric, 5: Quilt, 6: NeoForge
        mapping = {
            "forge": 1,
            "fabric": 4,
            "quilt": 5,
            "neoforge": 6
        }
        return mapping.get(mod_loader.lower(), 0)

