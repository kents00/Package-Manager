"""
Package Manager for Blender.

This module provides a UI panel and operators to manage Python packages
directly within Blender, supporting PyPI search, installation, and
requirements.txt bulk installation.
"""

__author__ = "Kent Edoloverio"
__version__ = "1.5.0"
__description__ = "A panel for managing Python packages directly within Blender."

import bpy
import re
import logging
import ssl
import sys
import os
import subprocess
import site
import time
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
from collections import OrderedDict
from importlib.metadata import distributions, distribution, PackageNotFoundError
from bpy_extras.io_utils import ImportHelper

try:
    from packaging.version import parse as parse_version
except ImportError:
    parse_version = None

# ---------------------------------------------------------------------------
# Legacy Addon Metadata (for Blender < 4.2 or legacy install fallback)
# ---------------------------------------------------------------------------

bl_info = {
    "name": "Package Manager",
    "blender": (4, 2, 0),
    "version": (1, 5, 0),
    "category": "Text Editor",
    "author": "Kent Edoloverio",
    "location": "Text Editor > Package Manager",
    "description": "A panel for managing Python packages directly within Blender.",
    "warning": "Once the package is installed/removed it requires restart to apply changes",
    "tracker_url": "https://github.com/kents00/Package-Manager/issues",
    "wiki_url": "https://github.com/kents00/Package-Manager",
}

# ---------------------------------------------------------------------------
# Caching & state
# ---------------------------------------------------------------------------

_pip_ensured = False
_installed_cache = None
_installed_names_set = None
_pypi_cache = OrderedDict()  # LRU cache: {query: (timestamp, result)}
_etag_cache = {}        # {url: etag_value}

_SSL_CONTEXT = ssl.create_default_context()
_ALLOWED_HOSTS = {"pypi.org"}

CACHE_TTL = 300         # 5 minutes
SEARCH_COOLDOWN = 2     # seconds between searches
REQUEST_TIMEOUT = 10    # seconds
MAX_CACHE_SIZE = 100    # Maximum number of items in _pypi_cache
PAGE_SIZE = 20          # Pagination page size for installed packages
SEARCH_INSTALLED_LABEL = "Search Installed Packages"

_REQ_LINE_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._-]*(\[.*\])?\s*(==|>=|<=|!=|~=|>|<)?\s*[a-zA-Z0-9.*,!=<>~\s]*$"
)

_last_search_time = 0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_package_name(name):
    """Validate package name to prevent injection/invalid characters."""
    if not name or not re.match(r"^[a-zA-Z0-9_\-]+$", name):
        raise ValueError(f"Invalid package name: {name}")


# ---------------------------------------------------------------------------
# PyPI helpers
# ---------------------------------------------------------------------------


def _clean_url(raw):
    """Internal helper to clean and validate a single URL string."""
    if not isinstance(raw, str):
        return None
    u = raw.strip()
    if not u:
        return None

    # Fix common typo "https//" or "http//"
    if u.startswith(("https//", "http//")):
        u = "https://" + (u[7:] if u.startswith("https//") else u[6:])

    # Basic URL validation: must start with http and contain a dot
    if u.startswith(("https://")) and "." in u:
        return u.rstrip(",").rstrip(";").rstrip(".")
    return None


def _format_author(name, email):
    """Format author name and email consistently."""
    n = (name or "").strip()
    e = (email or "").strip()
    if n and e:
        return f"{n} <{e}>"
    return n or e or "Unknown"


def _get_url_candidates(url_data):
    """Gather (label, url) pairs from various input formats."""
    if isinstance(url_data, dict):
        return list(url_data.items())
    candidates = []
    if isinstance(url_data, (list, tuple)):
        for item in url_data:
            if isinstance(item, str) and "," in item:
                parts = item.split(",", 1)
                candidates.append((parts[0].strip(), parts[1].strip()))
            else:
                candidates.append(("", str(item)))
    elif isinstance(url_data, str):
        candidates.extend([("", p) for p in re.split(r'[,\s]+', url_data)])
    return candidates


def extract_best_url(url_data):
    """
    Extract relevant URLs (home/repo and documentation) from various formats.
    Returns a dict: {"home": str, "docs": str}
    """
    candidates = _get_url_candidates(url_data)
    results = {"home": "", "docs": ""}
    docs_keywords = {"documentation", "docs", "wiki", "changelog"}

    cleaned_homes = []
    cleaned_docs = []

    for label, raw in candidates:
        u = _clean_url(raw)
        if not u:
            continue
        if any(kw in label.lower() for kw in docs_keywords):
            cleaned_docs.append(u)
        else:
            cleaned_homes.append(u)

    if cleaned_homes:
        gh = [u for u in cleaned_homes if "github.com" in u.lower()]
        results["home"] = gh[0] if gh else cleaned_homes[0]

    if cleaned_docs:
        results["docs"] = cleaned_docs[0]

    return results


def _fetch_pypi_json(url):
    """Network wrapper for PyPI JSON API with ETag support and SSL enforcement."""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in _ALLOWED_HOSTS:
        raise RuntimeError(f"Blocked request to untrusted host: {parsed.hostname}")

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if url in _etag_cache:
        req.add_header("If-None-Match", _etag_cache[url])

    try:
        # skipcq: BAN-B310
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_SSL_CONTEXT) as response:
            etag = response.headers.get("ETag")
            if etag:
                _etag_cache[url] = etag
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise RuntimeError(f"PyPI Error {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def _parse_pypi_result(info):
    """Transform PyPI info dict into our internal package format."""
    urls = info.get("project_urls") or {}
    if info.get("home_page"):
        urls["Homepage"] = info["home_page"]
    if info.get("project_url"):
        urls["Project"] = info["project_url"]

    extracted = extract_best_url(urls)
    author = _format_author(
        info.get("author") or info.get("maintainer"),
        info.get("author_email") or info.get("maintainer_email")
    )

    return [{
        "name": info["name"],
        "author": author,
        "version": info["version"],
        "summary": info.get("summary") or "No description available",
        "home_page": extracted["home"],
        "docs_url": extracted["docs"],
        "pkg_license": info.get("license") or "N/A",
    }]


def search_pypi(query):
    """Fetch package info from PyPI with LRU cache, ETag support, and timeout."""
    now = time.time()
    if query in _pypi_cache:
        cached_time, cached_result = _pypi_cache[query]
        if now - cached_time < CACHE_TTL:
            _pypi_cache.move_to_end(query)  # Mark as recently used
            return cached_result
        else:
            del _pypi_cache[query]  # Expired

    url = f"https://pypi.org/pypi/{query}/json"
    data = _fetch_pypi_json(url)

    if data is None:  # 304 Not Modified
        return _pypi_cache[query][1]

    result = _parse_pypi_result(data["info"])

    if len(_pypi_cache) >= MAX_CACHE_SIZE:
        _pypi_cache.popitem(last=False)  # Remove least recently used
    _pypi_cache[query] = (now, result)
    return result

# ---------------------------------------------------------------------------
# pip helpers
# ---------------------------------------------------------------------------


def ensure_pip():
    """Run ensurepip only once per session."""
    global _pip_ensured  # skipcq: PYL-W0603
    if _pip_ensured:
        return
    python_exec = sys.executable
    try:
        subprocess.check_call(
            [python_exec, "-m", "ensurepip", "--default-pip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pass  # pip may already be present
    _pip_ensured = True


def _handle_pip_error(package, result, operation="installation"):
    """Analyze pip output to provide helpful error suggestions."""
    err = (result.stderr or "") + (result.stdout or "")
    if not err:
        logger.error("Error during %s of %s: exit code %d", operation, package, result.returncode)
        return

    # List of (keywords, message) pairs
    patterns = [
        (["pkg-config"], f"Missing system tool: 'pkg-config'. {package} requires external C libraries."),
        (["Microsoft Visual C++", "cl.exe"], f"Missing build tools: {package} requires 'Microsoft Visual C++ Build Tools'."),
        (["Requires-Python"], f"Version mismatch: {package} is not compatible with this version of Python in Blender."),
        (["Could not find a version"], f"Not found: Could not find a version that satisfies the requirement '{package}'."),
        (["Conflicting dependencies", "ResolutionImpossible"], f"Dependency conflict: {package} has requirements that conflict with other installed packages."),
        (["ReadTimeoutError", "timed out"], f"Network timeout: The connection to PyPI timed out while downloading {package}."),
        (["SSL", "connection", "ProxyError"], f"Network error: Failed to download {package}. Check your internet/proxy settings."),
        (["No space left on device"], "Disk full: No space left on device to install the package."),
        (["PermissionError", "Access is denied"], f"Permission denied: Try running Blender as Administrator to {operation} {package}."),
        (["git not found", "mercurial not found", "svn not found"], f"Missing tool: A version control tool (git/hg/svn) required by {package} is not installed."),
    ]

    for keywords, msg in patterns:
        if any(kw.lower() in err.lower() for kw in keywords):
            logger.error(msg)
            return

    logger.error("Error during %s of %s:\n%s", operation, package, err)


def install_package(package):
    """Install a single package via pip (ensures pip once, not per call)."""
    try:
        try:
            distribution(package)
            logger.info("%s is already installed.", package)
            return True
        except PackageNotFoundError:
            pass
        logger.info("%s not found. Installing...", package)
        python_exec = sys.executable

        try:
            ensure_pip()
            result = subprocess.run(
                [python_exec, "-m", "pip", "install", "--user", "--", package],
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode != 0:
                _handle_pip_error(package, result)
                return False

            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.append(user_site)
                logger.info("Added %s to sys.path", user_site)

            # Verify installation via metadata check (case-insensitive for package name)
            try:
                distribution(package)
                _installed_cache = None  # invalidate cache
                logger.info("%s installed successfully.", package)
                return True
            except PackageNotFoundError:
                logger.error("Failed to verify installation of %s after pip reported success.", package)
                return False
        except subprocess.CalledProcessError as e:
            logger.error("Error during installation of %s: %s", package, e)
            return False
    except Exception as e:
        logger.error("Unexpected error during installation of %s: %s", package, e)
        return False


def install_packages_from_requirements(file_path):
    """Install all packages from a requirements file in a single pip call."""
    if not os.path.isfile(file_path):
        logger.warning("Requirements file not found: %s", file_path)
        return False

    with open(file_path, "r") as f:
        all_lines = [
            r.strip()
            for r in f
            if r.strip() and not r.startswith("#")
        ]

    requirements = [r for r in all_lines if _REQ_LINE_RE.match(r)]
    rejected = [r for r in all_lines if not _REQ_LINE_RE.match(r)]
    if rejected:
        logger.warning("Rejected %d suspicious lines from requirements: %s", len(rejected), rejected[:5])

    # Warn about unpinned packages
    unpinned = [r for r in requirements if "==" not in r]
    if unpinned:
        logger.warning("%d packages have no version pin. This may cause instability.", len(unpinned))

    if not requirements:
        logger.warning("No packages found in requirements file.")
        return False

    ensure_pip()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "--"] + requirements,
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            _handle_pip_error("requirements file", result, "bulk installation")
            return False

        logger.info("All packages installed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Error during bulk installation: %s", e)
        return False

# ---------------------------------------------------------------------------
# Installed packages helpers
# ---------------------------------------------------------------------------


def get_installed_packages(force_refresh=False):
    """Return cached list of installed packages; refresh on demand."""
    global _installed_cache  # skipcq: PYL-W0603
    if _installed_cache is not None and not force_refresh:
        return _installed_cache

    user_site = site.getusersitepackages().lower()

    _installed_cache = []
    for d in distributions():
        meta = d.metadata
        urls = []
        if meta.get("Home-page"):
            urls.append(meta.get("Home-page"))
        project_urls = meta.get_all("Project-URL")
        if project_urls:
            urls.extend(project_urls)

        extracted = extract_best_url(urls)

        # Detect if it's a system package (not in user site-packages)
        location = str(d.locate_file("")).lower()
        is_system = user_site not in location

        _installed_cache.append({
            "name": meta["Name"],
            "version": meta["Version"],
            "author": _format_author(meta.get("Author"), meta.get("Author-email")),
            "summary": meta.get("Summary") or "No description available",
            "home_page": extracted["home"],
            "docs_url": extracted["docs"],
            "is_system": is_system,
        })

    return _installed_cache


def get_installed_names_set(force_refresh=False):
    """Return a cached set of lowercase installed package names."""
    global _installed_names_set  # skipcq: PYL-W0603
    if _installed_names_set is None or force_refresh:
        _installed_names_set = {pkg["name"].lower() for pkg in get_installed_packages(force_refresh)}
    return _installed_names_set


def uninstall_package(package):
    """Uninstall a package via pip and invalidate cache."""
    global _installed_cache  # skipcq: PYL-W0603
    python_exec = sys.executable

    try:
        installed_packages = get_installed_packages(force_refresh=True)
        installed_names = [pkg["name"].lower() for pkg in installed_packages]

        if package.lower() not in installed_names:
            logger.warning("Package %s not found. Skipping uninstall.", package)
            return False

        # Use run instead of check_call to capture output for better error detection
        result = subprocess.run(
            [python_exec, "-m", "pip", "uninstall", "-y", "--", package],
            capture_output=True,
            text=True,
            check=False
        )

        if result.returncode != 0:
            err = result.stderr or result.stdout or ""
            if "PermissionError" in err or "Access is denied" in err:
                logger.error("Permission denied: Could not uninstall %s. "
                             "You may need to run Blender as Administrator if it's in a system directory.", package)
            else:
                logger.error("Error during uninstallation: %s", err)
            return False

        _installed_cache = None  # invalidate cache
        _installed_names_set = None  # invalidate names cache
        logger.info("%s uninstalled successfully.", package)
        return True
    except Exception as e:
        logger.error("Unexpected error during uninstallation: %s", e)
        return False


# ---------------------------------------------------------------------------
# Auto-update logic
# ---------------------------------------------------------------------------

_update_timer_registered = False


def check_package_update(package_name):
    """Check PyPI for a newer version of a package."""
    try:
        url = f"https://pypi.org/pypi/{package_name}/json"
        data = _fetch_pypi_json(url)
        if data:
            return data["info"]["version"]
    except Exception:
        pass
    return None


def update_checker_timer():
    """Timer callback to trigger background update checks."""
    # This timer runs in the main thread, so it can safely access bpy.context.scene
    bg_update_check()
    return 21600  # Run every 6 hours


def bg_update_check():
    """Trigger background check for all installed packages with auto-update on."""
    packages_to_check = []

    # This must run in main thread (context access)
    for pkg in bpy.context.scene.installed_package_list:
        if pkg.auto_update:
            packages_to_check.append((pkg.name, pkg.version))

    def _worker(pkgs):
        """Background thread worker to check for updates on PyPI."""
        results = []
        for name, current_v in pkgs:
            latest_v = check_package_update(name)
            if latest_v:
                if parse_version and parse_version(latest_v) > parse_version(current_v):
                    results.append((name, latest_v))
                elif not parse_version and latest_v != current_v:
                    results.append((name, latest_v))

        # Schedule the UI update back on main thread
        if results:
            bpy.app.timers.register(lambda: apply_update_results(results), first_interval=0.1)

    threading.Thread(target=_worker, args=(packages_to_check,), daemon=True).start()
    return 21600  # 6 hours


def apply_update_results(results):
    """Apply found updates to the property groups (Main Thread)."""
    for name, latest_v in results:
        for pkg in bpy.context.scene.installed_package_list:
            if pkg.name == name:
                pkg.latest_version = latest_v
                pkg.update_available = True
                break

# ---------------------------------------------------------------------------
# UI — Panel
# ---------------------------------------------------------------------------


class PackageManagementPanel(bpy.types.Panel):
    """Package Manager — main parent panel"""
    bl_label = "Package Manager"
    bl_idname = "TEXT_PT_package_manager"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_category = "Package Manager"

    def draw(self, context):
        """Draw the parent panel header."""
        layout = self.layout
        layout.label(text=f"v{__version__}", icon="FILE_SCRIPT")

    @staticmethod
    def draw_package_box(layout, package, is_installed=False, installed_set=None):
        """Helper to draw a single package information box."""
        box = layout.box()
        row = box.row()

        if is_installed and package.is_system:
            row.label(text=package.name, icon="LOCKED")
        else:
            row.label(text=package.name, icon="FILE_SCRIPT")
        row.label(text=f"v{package.version}")

        box.row().label(text=f"Author: {package.author}", icon="USER")
        box.row().label(text=package.description, icon="INFO")

        row = box.row()
        if is_installed and package.update_available:
            row.label(text=f"Update available: v{package.latest_version}", icon="FILE_REFRESH")
            row.operator("wm.download_package", text="Update", icon="IMPORT").package_name = package.name

        row = box.row()
        if package.url or package.docs_url:
            if package.url:
                op = row.operator("wm.url_open", text="Website", icon="URL")
                op.url = package.url
            if package.docs_url:
                op = row.operator("wm.url_open", text="Docs", icon="BOOKMARKS")
                op.url = package.docs_url

        if is_installed:
            row.operator("wm.uninstall_package", text="Uninstall", icon="TRASH").package_name = package.name
            row.prop(package, "auto_update", text="Auto Update")
        else:
            if package.name.lower() in (installed_set or set()):
                row.label(text="Installed", icon="CHECKMARK")
            else:
                row.operator("wm.download_package", text="Download", icon="IMPORT").package_name = package.name


class PackageSearchPanel(bpy.types.Panel):
    """Search PyPI for packages"""
    bl_label = "Search PyPI"
    bl_idname = "TEXT_PT_package_manager_search"
    bl_parent_id = "TEXT_PT_package_manager"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Draw the search sub-panel."""
        layout = self.layout
        scene = context.scene

        row = layout.row()
        row.prop(scene, "search_query", text="")
        row.operator("wm.search_packages", text="Search")

        if scene.package_list:
            installed_set = get_installed_names_set()
            for package in scene.package_list:
                PackageManagementPanel.draw_package_box(layout, package, installed_set=installed_set)
        else:
            if scene.search_query.strip():
                layout.label(text=f"No packages found for '{scene.search_query}'.", icon="INFO")
            else:
                layout.label(text="Enter a package name above to search PyPI.", icon="INFO")


class PackageInstalledPanel(bpy.types.Panel):
    """View and manage installed packages"""
    bl_label = "Installed Packages"
    bl_idname = "TEXT_PT_package_manager_installed"
    bl_parent_id = "TEXT_PT_package_manager"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Draw the installed packages sub-panel with sort and pagination."""
        layout = self.layout
        scene = context.scene

        row = layout.row(align=True)
        row.prop(scene, "installed_search_query", text="", icon="VIEWZOOM")
        row.prop(scene, "installed_sort_order", text="")
        row.operator("wm.refresh_installed_packages", text="", icon="FILE_REFRESH")

        query = scene.installed_search_query.lower()
        filtered = [pkg for pkg in scene.installed_package_list if query in pkg.name.lower()]

        # Sort
        sort = scene.installed_sort_order
        if sort == "NAME_AZ":
            filtered.sort(key=lambda p: p.name.lower())
        elif sort == "NAME_ZA":
            filtered.sort(key=lambda p: p.name.lower(), reverse=True)

        if not filtered:
            if query:
                layout.label(text=f"No packages match '{scene.installed_search_query}'.", icon="INFO")
            else:
                layout.label(text="No packages installed via pip --user.", icon="INFO")
                layout.label(text="Click refresh to scan.", icon="QUESTION")
            return

        # Pagination
        total = len(filtered)
        page = scene.installed_page_index
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
            scene.installed_page_index = page

        start = page * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)
        page_items = filtered[start:end]

        for package in page_items:
            PackageManagementPanel.draw_package_box(layout, package, is_installed=True)

        if total_pages > 1:
            row = layout.row(align=True)
            sub = row.row(align=True)
            sub.enabled = page > 0
            sub.operator("wm.installed_page_prev", text="", icon="TRIA_LEFT")
            row.label(text=f"Page {page + 1} / {total_pages}  ({total} packages)")
            sub = row.row(align=True)
            sub.enabled = page < total_pages - 1
            sub.operator("wm.installed_page_next", text="", icon="TRIA_RIGHT")


class PackageBulkPanel(bpy.types.Panel):
    """Bulk install from requirements.txt"""
    bl_label = "Bulk Download"
    bl_idname = "TEXT_PT_package_manager_bulk"
    bl_parent_id = "TEXT_PT_package_manager"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        """Draw the bulk download sub-panel."""
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "bulk_download_path", text="Path")
        row = layout.row()
        row.operator("wm.file_select", text="Choose File")
        row.operator("wm.bulk_download_packages", text="Install All")

# ---------------------------------------------------------------------------
# UI — Operators
# ---------------------------------------------------------------------------


class WM_OT_FileSelect(bpy.types.Operator, ImportHelper):
    """Operator to open the file browser"""
    bl_idname = "wm.file_select"
    bl_label = "Select Bulk Download Path"

    filter_glob: bpy.props.StringProperty(default="*", options={"HIDDEN"})

    def execute(self, context):
        """Update the scene property with the selected file path."""
        context.scene.bulk_download_path = self.filepath
        self.report({"INFO"}, f"Selected path: {self.filepath}")
        return {"FINISHED"}


class WM_OT_SearchPackages(bpy.types.Operator):
    """Search PyPI for packages (with debounce and caching)"""
    bl_idname = "wm.search_packages"
    bl_label = "Search Packages"

    _thread = None
    _results = None
    _error = None

    def modal(self, context, event):
        """Handle the modal execution state (waiting for thread)."""
        if self._thread and not self._thread.is_alive():
            context.scene.package_list.clear()
            if self._error:
                self.report(
                    {"ERROR"}, f"Error fetching results: {self._error}")
            elif self._results:
                for result in self._results:
                    item = context.scene.package_list.add()
                    item.name = result["name"]
                    item.version = result["version"]
                    item.author = result["author"]
                    item.description = result["summary"]
                    item.url = result["home_page"]
                    item.docs_url = result.get("docs_url", "")
                    item.auto_update = False
                self.report(
                    {"INFO"}, f"Found {len(self._results)} packages."
                )
            else:
                self.report({"INFO"}, "No results found.")
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        """Start the search in a background thread."""
        global _last_search_time  # skipcq: PYL-W0603

        # Debounce
        now = time.time()
        if now - _last_search_time < SEARCH_COOLDOWN:
            self.report({"WARNING"}, "Please wait before searching again.")
            return {"CANCELLED"}
        _last_search_time = now

        query = context.scene.search_query
        if not query.strip():
            self.report({"WARNING"}, "Please enter a search query.")
            return {"CANCELLED"}

        self._results = None
        self._error = None

        def _search():
            """Background thread worker to perform PyPI search."""
            try:
                self._results = search_pypi(query)
            except Exception as e:
                self._error = str(e)

        self._thread = threading.Thread(target=_search, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}


class WM_OT_RefreshInstalledPackages(bpy.types.Operator):
    """Refresh the list of installed packages (forces cache refresh)"""
    bl_idname = "wm.refresh_installed_packages"
    bl_label = "Refresh Installed Packages"

    def execute(self, context):
        """Refresh the installed packages list."""
        context.scene.installed_package_list.clear()
        installed_packages = get_installed_packages(force_refresh=True)

        for package in installed_packages:
            item = context.scene.installed_package_list.add()
            item.name = package["name"]
            item.version = package["version"]
            item.author = package["author"]
            item.description = package["summary"]
            item.url = package["home_page"]
            item.docs_url = package.get("docs_url", "")
            item.is_system = package.get("is_system", False)

        self.report(
            {"INFO"}, f"Found {len(installed_packages)} installed packages."
        )
        return {"FINISHED"}


class WM_OT_DownloadPackage(bpy.types.Operator):
    """Download and install a package in a background thread"""
    bl_idname = "wm.download_package"
    bl_label = "Download Package"

    package_name: bpy.props.StringProperty()

    _thread = None
    _result = None

    def modal(self, context, event):
        """Handle the modal execution state (waiting for thread)."""
        if self._thread and not self._thread.is_alive():
            if self._result:
                self.report(
                    {"INFO"}, f"{self.package_name} installed successfully."
                )
                # Clear update flag if it was an upgrade
                for item in context.scene.installed_package_list:
                    if item.name == self.package_name:
                        item.update_available = False
                        # Force a refresh to get the new version number
                        bpy.ops.wm.refresh_installed_packages()
                        break
            else:
                self.report(
                    {"ERROR"}, f"Failed to install {self.package_name}."
                )
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        """Start the install in a background thread."""
        self._result = None

        def _install():
            """Background thread worker to install a package."""
            try:
                validate_package_name(self.package_name)
                self._result = install_package(self.package_name)
            except ValueError as e:
                logger.error("Validation error: %s", e)
                self._result = False

        self._thread = threading.Thread(target=_install, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Installing {self.package_name}...")
        return {"RUNNING_MODAL"}


class WM_OT_UninstallPackage(bpy.types.Operator):
    """Uninstall a package after confirmation"""
    bl_idname = "wm.uninstall_package"
    bl_label = "Uninstall Package"
    bl_description = "Remove this package from Blender's user site-packages. Requires a restart to fully apply."

    package_name: bpy.props.StringProperty()

    def invoke(self, context, event):
        """Show a confirmation dialog before uninstalling."""
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        """Uninstall the selected package."""
        package_name = self.package_name

        try:
            validate_package_name(package_name)
        except ValueError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}

        if uninstall_package(package_name):
            self.report({"INFO"}, f"{package_name} uninstalled successfully.")
        else:
            self.report({"ERROR"}, f"Failed to uninstall {package_name}.")
        return {"FINISHED"}


class WM_OT_BulkDownloadPackages(bpy.types.Operator):
    """Bulk install packages from a requirements file in a background thread"""
    bl_idname = "wm.bulk_download_packages"
    bl_label = "Bulk Download Packages"

    _thread = None
    _result = None

    def modal(self, context, event):
        """Handle the modal execution state (waiting for thread)."""
        if self._thread and not self._thread.is_alive():
            file_path = context.scene.bulk_download_path
            if self._result:
                self.report(
                    {"INFO"},
                    f"Packages from {file_path} installed successfully.",
                )
            else:
                self.report(
                    {"ERROR"},
                    f"Failed to install packages from {file_path}.",
                )
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        """Start the bulk install in a background thread."""
        file_path = context.scene.bulk_download_path
        if not file_path:
            self.report({"ERROR"}, "No file path selected.")
            return {"CANCELLED"}

        self._result = None

        def _bulk():
            """Background thread worker to perform bulk installation."""
            self._result = install_packages_from_requirements(file_path)

        self._thread = threading.Thread(target=_bulk, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Installing packages from {file_path}...")
        return {"RUNNING_MODAL"}


class WM_OT_SearchInstalledPackages(bpy.types.Operator):
    """Filter the installed packages list by search query"""
    bl_idname = "wm.search_installed_packages"
    bl_label = SEARCH_INSTALLED_LABEL

    def execute(self, context):  # skipcq: PYL-R0201
        """Filter the list based on search query."""
        search_query = context.scene.installed_search_query.lower()
        for item in context.scene.installed_package_list:
            item.hide = search_query not in item.name.lower()
        return {"FINISHED"}


class WM_OT_InstalledPagePrev(bpy.types.Operator):
    """Show the previous page of installed packages"""
    bl_idname = "wm.installed_page_prev"
    bl_label = "Previous Page"

    def execute(self, context):
        """Go to previous page."""
        scene = context.scene
        if scene.installed_page_index > 0:
            scene.installed_page_index -= 1
        return {"FINISHED"}


class WM_OT_InstalledPageNext(bpy.types.Operator):
    """Show the next page of installed packages"""
    bl_idname = "wm.installed_page_next"
    bl_label = "Next Page"

    def execute(self, context):
        """Go to next page."""
        context.scene.installed_page_index += 1
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PackageItem(bpy.types.PropertyGroup):
    """Property group representing a package in the list."""
    name: bpy.props.StringProperty()
    version: bpy.props.StringProperty()
    author: bpy.props.StringProperty()
    description: bpy.props.StringProperty()
    url: bpy.props.StringProperty()
    docs_url: bpy.props.StringProperty()
    auto_update: bpy.props.BoolProperty()
    latest_version: bpy.props.StringProperty()
    update_available: bpy.props.BoolProperty(default=False)
    is_system: bpy.props.BoolProperty()
    hide: bpy.props.BoolProperty(default=False)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


classes = (
    PackageItem,
    PackageManagementPanel,
    PackageSearchPanel,
    PackageInstalledPanel,
    PackageBulkPanel,
    WM_OT_SearchPackages,
    WM_OT_RefreshInstalledPackages,
    WM_OT_DownloadPackage,
    WM_OT_UninstallPackage,
    WM_OT_BulkDownloadPackages,
    WM_OT_FileSelect,
    WM_OT_SearchInstalledPackages,
    WM_OT_InstalledPagePrev,
    WM_OT_InstalledPageNext,
)


def register():
    """Register all classes and properties."""
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.search_query = bpy.props.StringProperty(
        name="Search Query",
        description="Enter a PyPI package name to search for"
    )
    bpy.types.Scene.bulk_download_path = bpy.props.StringProperty(
        name="Bulk Download Path",
        description="Path to a requirements.txt file for bulk installation"
    )
    bpy.types.Scene.package_list = bpy.props.CollectionProperty(
        type=PackageItem
    )
    bpy.types.Scene.installed_package_list = bpy.props.CollectionProperty(
        type=PackageItem
    )
    bpy.types.Scene.installed_search_query = bpy.props.StringProperty(
        name=SEARCH_INSTALLED_LABEL,
        description="Filter installed packages by name"
    )
    bpy.types.Scene.installed_sort_order = bpy.props.EnumProperty(
        name="Sort Order",
        description="Sort installed packages",
        items=[
            ("NAME_AZ", "A → Z", "Sort alphabetically ascending"),
            ("NAME_ZA", "Z → A", "Sort alphabetically descending"),
        ],
        default="NAME_AZ",
    )
    bpy.types.Scene.installed_page_index = bpy.props.IntProperty(
        name="Page",
        default=0,
        min=0,
        description="Current page of installed packages"
    )

    # Register background update timer
    if not bpy.app.timers.is_registered(bg_update_check):
        bpy.app.timers.register(bg_update_check, first_interval=60.0)


def unregister():
    """Unregister all classes and properties."""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.search_query
    del bpy.types.Scene.bulk_download_path
    del bpy.types.Scene.package_list
    del bpy.types.Scene.installed_package_list
    del bpy.types.Scene.installed_search_query
    del bpy.types.Scene.installed_sort_order
    del bpy.types.Scene.installed_page_index

    # Unregister timers
    if bpy.app.timers.is_registered(bg_update_check):
        bpy.app.timers.unregister(bg_update_check)


if __name__ == "__main__" and hasattr(bpy.utils, "register_class"):
    register()
