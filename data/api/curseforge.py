"""
CurseForge API provider implementation.
"""

import logging
import os
import requests
import time
from typing import Dict, List, Optional, Any
from pathlib import Path

from tqdm import tqdm

from data.api.base import BaseProvider

# CurseForge API constants
CURSEFORGE_API_BASE = "https://api.curseforge.com/v1"
CURSEFORGE_GAME_ID = 432  # Minecraft game ID

# Built-in public API key shared by several open-source Minecraft launchers
# (e.g. Prism Launcher).  This allows CurseForge to work out of the box
# without requiring users to supply their own key.  Users can override it by
# setting the CURSEFORGE_API_KEY environment variable or via the config file.
CURSEFORGE_DEFAULT_API_KEY = "$2a$10$bL4bIL5pUWqfcO7KwT2NleecEFV7SqMGqaGFRZLfRMOSMTWrIGaSq"


class CurseForgeProvider(BaseProvider):
    """Provider for interacting with the CurseForge API."""
    
    def __init__(self, api_key: str = ""):
        """
        Initialize the CurseForge provider.
        
        Key resolution order (first non-empty value wins):
        1. ``api_key`` argument (from config file)
        2. ``CURSEFORGE_API_KEY`` environment variable
        3. Built-in default key
        
        Args:
            api_key: Optional personal CurseForge API key from the config file.
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
        
        self.headers = {
            "Accept": "application/json",
            "x-api-key": self.api_key
        }
        self.max_retries = 3
        self.retry_delay = 1  # seconds
    
    def get_project_id(self, mod_id: str) -> Optional[str]:
        """
        Get the CurseForge project ID for a mod.
        
        Args:
            mod_id: The mod ID to look up
            
        Returns:
            CurseForge project ID or None if not found
        """
        try:
            url = f"{CURSEFORGE_API_BASE}/mods/search"
            params = {
                "gameId": CURSEFORGE_GAME_ID,
                "searchFilter": mod_id,
                "classId": 6,  # Mod class ID
                "pageSize": 5
            }
            
            response = self._make_request("GET", url, params=params)
            if not response:
                return None
                
            data = response.json()
            results = data.get('data', [])
            
            # Try to find an exact match first
            for result in results:
                if result.get('slug') == mod_id or result.get('name').lower() == mod_id.lower():
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
            url = f"{CURSEFORGE_API_BASE}/mods/{project_id}/files"
            params = {
                "gameVersion": game_version,
                "modLoaderType": self._map_mod_loader_to_curseforge(mod_loader),
                "pageSize": 20  # Get a reasonable number of files to sort through
            }
            
            response = self._make_request("GET", url, params=params)
            if not response:
                return None
                
            data = response.json()
            files = data.get('data', [])
            
            if not files:
                self.logger.warning(f"No files found for CurseForge project {project_id}")
                return None
                
            # Filter for files compatible with our game version
            filtered_files = []
            for file in files:
                game_versions = file.get('gameVersions', [])
                is_compatible = (
                    game_version in game_versions and 
                    file.get('isAvailable', True) and
                    not file.get('isServerPack', False)
                )
                
                if is_compatible:
                    filtered_files.append(file)
            
            if not filtered_files:
                self.logger.warning(
                    f"No compatible files found for CurseForge project {project_id} "
                    f"with Minecraft {game_version} and {mod_loader}"
                )
                return None
            
            # Sort by fileDate descending to get the latest
            filtered_files.sort(key=lambda x: x.get('fileDate', ''), reverse=True)
            latest_file = filtered_files[0]
            
            # Construct response in a standard format
            download_url = latest_file.get('downloadUrl')
            file_id = latest_file.get('id')
            
            return {
                'version_number': latest_file.get('displayName', '').split('-')[-1].strip(),
                'version_id': str(file_id),
                'date_published': latest_file.get('fileDate'),
                'game_versions': latest_file.get('gameVersions', []),
                'project_id': project_id,
                'files': [{'url': download_url if download_url else f"curseforge:{file_id}"}],
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
            # Get download URL from version info
            files = version_info.get('files', [])
            if not files:
                self.logger.warning("No download files available in version info")
                return False
                
            # Use the primary file (first in the list)
            download_url = files[0].get('url')
            if not download_url:
                self.logger.warning("No download URL available in version info")
                return False
            
            # Handle special curseforge: URLs
            if download_url.startswith("curseforge:"):
                file_id = download_url.replace("curseforge:", "")
                download_url = self._get_direct_download_url(file_id)
                
                if not download_url:
                    self.logger.error(f"Failed to get direct download URL for file {file_id}")
                    return False
            
            # Download the file
            response = self._make_request("GET", download_url, stream=True)
            if not response:
                return False
                
            # Get file size for progress bar
            total_size = int(response.headers.get('content-length', 0))
            
            # Ensure directory exists
            output_dir = Path(output_path).parent
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Download with progress bar
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
    
    def _get_direct_download_url(self, file_id: str) -> Optional[str]:
        """
        Get a direct download URL for a CurseForge file.
        
        Args:
            file_id: CurseForge file ID
            
        Returns:
            Direct download URL or None if not available
        """
        try:
            url = f"{CURSEFORGE_API_BASE}/mods/files/{file_id}/download-url"
            response = self._make_request("GET", url)
            if not response:
                return None
                
            data = response.json()
            return data.get('data')
        except Exception as e:
            self.logger.error(f"Error getting direct download URL for file {file_id}: {str(e)}")
            return None
    
    def _map_mod_loader_to_curseforge(self, mod_loader: str) -> int:
        """
        Map mod loader string to CurseForge mod loader type ID.
        
        Args:
            mod_loader: String identifier for mod loader (fabric, forge, quilt)
            
        Returns:
            CurseForge mod loader type ID
        """
        # CurseForge mod loader type IDs
        # 1: Forge, 4: Fabric, 5: Quilt, 6: NeoForge
        mapping = {
            "forge": 1,
            "fabric": 4,
            "quilt": 5,
            "neoforge": 6
        }
        return mapping.get(mod_loader.lower(), 0)  # 0 means Any
    
    def _make_request(
        self, 
        method: str, 
        url: str, 
        params: Optional[Dict[str, Any]] = None, 
        stream: bool = False
    ) -> Optional[requests.Response]:
        """
        Make an HTTP request with retry logic.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: URL to request
            params: Query parameters
            stream: Whether to stream the response
            
        Returns:
            Response object or None if request failed
        """
        # Only send the API key to CurseForge API endpoints, not CDN download URLs
        headers = self.headers if url.startswith(CURSEFORGE_API_BASE) else {}

        for attempt in range(self.max_retries):
            try:
                response = requests.request(
                    method, 
                    url, 
                    params=params, 
                    headers=headers,
                    stream=stream
                )
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                self.logger.warning(
                    f"Request failed (attempt {attempt+1}/{self.max_retries}): {str(e)}"
                )
                
                # Check if we should retry
                if attempt < self.max_retries - 1:
                    # Implement exponential backoff
                    wait_time = self.retry_delay * (2 ** attempt)
                    self.logger.info(f"Waiting {wait_time}s before retrying...")
                    time.sleep(wait_time)
                else:
                    # Last attempt failed
                    self.logger.error(f"All {self.max_retries} requests failed: {str(e)}")
                    return None
        
        return None  # This should never be reached due to the return in the exception handler

