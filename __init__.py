import bpy
import sys
import os
import subprocess
import site
import time
import json
import threading
import urllib.request
import urllib.error
from importlib.metadata import distributions
from bpy_extras.io_utils import ImportHelper

# ---------------------------------------------------------------------------
# Legacy Addon Metadata (for Blender < 4.2 or legacy install fallback)
# ---------------------------------------------------------------------------

bl_info = {
    "name": "Package Manager",
    "blender": (4, 2, 0),
    "version": (1, 4, 0),
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
_pypi_cache = {}        # {query: (timestamp, result)}
_etag_cache = {}        # {url: etag_value}

CACHE_TTL = 300         # 5 minutes
SEARCH_COOLDOWN = 2     # seconds between searches
REQUEST_TIMEOUT = 10    # seconds

_last_search_time = 0

# ---------------------------------------------------------------------------
# PyPI helpers
# ---------------------------------------------------------------------------


def search_pypi(query):
    """Fetch package info from PyPI with TTL cache, ETag support, and timeout."""
    now = time.time()

    # Check in-memory cache
    if query in _pypi_cache:
        cached_time, cached_result = _pypi_cache[query]
        if now - cached_time < CACHE_TTL:
            return cached_result

    url = f"https://pypi.org/pypi/{query}/json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    # ETag conditional request
    if url in _etag_cache:
        req.add_header("If-None-Match", _etag_cache[url])

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            etag = response.headers.get("ETag")
            if etag:
                _etag_cache[url] = etag
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Not Modified — return cached result
            cached = _pypi_cache.get(query)
            if cached:
                return cached[1]
        raise RuntimeError(
            f"Error fetching package details from PyPI. Status code: {e.code}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error contacting PyPI: {e.reason}") from e

    info = data["info"]
    result = [
        {
            "name": info["name"],
            "author": info.get("author") or "Unknown",
            "version": info["version"],
            "pkg_license": info.get("license") or "N/A",
        }
    ]

    _pypi_cache[query] = (now, result)
    return result

# ---------------------------------------------------------------------------
# pip helpers
# ---------------------------------------------------------------------------


def ensure_pip():
    """Run ensurepip only once per session."""
    global _pip_ensured
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


def install_package(package):
    """Install a single package via pip (ensures pip once, not per call)."""
    try:
        __import__(package)
        print(f"{package} is already installed.")
        return True
    except ImportError:
        print(f"{package} not found. Installing...")
        python_exec = sys.executable

        try:
            ensure_pip()
            subprocess.check_call(
                [python_exec, "-m", "pip", "install", package]
            )

            user_site = site.getusersitepackages()
            if user_site not in sys.path:
                sys.path.append(user_site)
                print(f"Added {user_site} to sys.path")

            __import__(package)
            print(f"{package} installed successfully.")
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error during installation: {e}")
            return False
        except ImportError as e:
            print(f"Failed to import {package} after installation. Error: {e}")
            print("sys.path:", sys.path)
            return False


def install_packages_from_requirements(file_path):
    """Install all packages from a requirements file in a single pip call."""
    if not os.path.isfile(file_path):
        print(f"Requirements file not found: {file_path}")
        return False

    with open(file_path, "r") as f:
        requirements = [
            r.strip()
            for r in f
            if r.strip() and not r.startswith("#")
        ]

    if not requirements:
        print("No packages found in requirements file.")
        return False

    ensure_pip()
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install"] + requirements
        )
        print("All packages installed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error during bulk installation: {e}")
        return False

# ---------------------------------------------------------------------------
# Installed packages helpers
# ---------------------------------------------------------------------------


def get_installed_packages(force_refresh=False):
    """Return cached list of installed packages; refresh on demand."""
    global _installed_cache
    if _installed_cache is not None and not force_refresh:
        return _installed_cache

    _installed_cache = [
        {"name": d.metadata["Name"], "version": d.metadata["Version"]}
        for d in distributions()
    ]
    return _installed_cache


def uninstall_package(package):
    """Uninstall a package via pip and invalidate cache."""
    global _installed_cache
    python_exec = sys.executable

    try:
        installed_packages = get_installed_packages(force_refresh=True)
        installed_names = [pkg["name"].lower() for pkg in installed_packages]

        if package.lower() not in installed_names:
            print(f"Package {package} not found. Skipping uninstall.")
            return False

        subprocess.check_call(
            [python_exec, "-m", "pip", "uninstall", "-y", package]
        )
        _installed_cache = None  # invalidate cache
        print(f"{package} uninstalled successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error during uninstallation: {e}")
        return False

# ---------------------------------------------------------------------------
# UI — Panel
# ---------------------------------------------------------------------------


class PackageManagementPanel(bpy.types.Panel):
    """Creates a Panel in the Text Editor side panel"""
    bl_label = "Package Manager"
    bl_idname = "TEXT_PT_package_manager"
    bl_space_type = "TEXT_EDITOR"
    bl_region_type = "UI"
    bl_category = "Package Manager"

    def draw(self, context):
        layout = self.layout

        layout.label(text="Search Packages:")
        row = layout.row()
        row.prop(context.scene, "search_query", text="")
        row.operator("wm.search_packages", text="Search")

        layout.label(text="Results:")
        box = layout.box()
        row = box.row()
        row.prop(
            context.scene,
            "show_search_results",
            text="Show Search Results",
            icon="TRIA_DOWN"
            if context.scene.show_search_results
            else "TRIA_RIGHT",
        )
        if context.scene.show_search_results:
            if context.scene.package_list:
                for package in context.scene.package_list:
                    box = layout.box()
                    row = box.row()
                    row.label(text=package.name, icon="FILE_SCRIPT")
                    row.label(text=f"v{package.version}")
                    row = box.row()
                    row.operator(
                        "wm.download_package", text="Download"
                    ).package_name = package.name
            else:
                box.label(text="No results found.")

        layout.separator()
        layout.label(text="Installed Packages:")

        row = layout.row()
        row.prop(
            context.scene,
            "installed_search_query",
            text="Search Installed Packages",
        )
        row.operator("wm.search_installed_packages", text="Search")

        layout.operator("wm.refresh_installed_packages", text="Refresh")

        box = layout.box()
        row = box.row()
        row.prop(
            context.scene,
            "show_installed_packages",
            text="Show Installed Packages",
            icon="TRIA_DOWN"
            if context.scene.show_installed_packages
            else "TRIA_RIGHT",
        )
        if context.scene.show_installed_packages:
            filtered_packages = [
                pkg
                for pkg in context.scene.installed_package_list
                if context.scene.installed_search_query.lower()
                in pkg.name.lower()
            ]
            if filtered_packages:
                for package in filtered_packages:
                    box = layout.box()
                    row = box.row()
                    row.label(text=package.name, icon="FILE_SCRIPT")
                    row.label(text=f"v{package.version}")

                    row = box.row()
                    row.operator(
                        "wm.disable_package", text="Disable"
                    ).package_name = package.name
                    row.prop(package, "auto_update", text="Auto Update")
            else:
                box.label(
                    text="No installed packages match the search query."
                )

        layout.separator()
        layout.label(text="Bulk Download Packages:")
        layout.prop(context.scene, "bulk_download_path", text="Selected Path")
        layout.operator("wm.file_select", text="Choose File Path")
        layout.operator("wm.bulk_download_packages", text="Download All")

# ---------------------------------------------------------------------------
# UI — Operators
# ---------------------------------------------------------------------------


class WM_OT_FileSelect(bpy.types.Operator, ImportHelper):
    """Operator to open the file browser"""
    bl_idname = "wm.file_select"
    bl_label = "Select Bulk Download Path"

    filter_glob: bpy.props.StringProperty(default="*", options={"HIDDEN"})

    def execute(self, context):
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
        global _last_search_time

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
        context.scene.installed_package_list.clear()
        installed_packages = get_installed_packages(force_refresh=True)

        for package in installed_packages:
            item = context.scene.installed_package_list.add()
            item.name = package["name"]
            item.version = package["version"]

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
        if self._thread and not self._thread.is_alive():
            if self._result:
                self.report(
                    {"INFO"}, f"{self.package_name} installed successfully."
                )
            else:
                self.report(
                    {"ERROR"}, f"Failed to install {self.package_name}."
                )
            context.area.tag_redraw()
            return {"FINISHED"}
        return {"PASS_THROUGH"}

    def execute(self, context):
        self._result = None

        def _install():
            self._result = install_package(self.package_name)

        self._thread = threading.Thread(target=_install, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Installing {self.package_name}...")
        return {"RUNNING_MODAL"}


class WM_OT_DisablePackage(bpy.types.Operator):
    """Uninstall a package"""
    bl_idname = "wm.disable_package"
    bl_label = "Disable Package"

    package_name: bpy.props.StringProperty()

    def execute(self, context):
        package_name = self.package_name
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
        file_path = context.scene.bulk_download_path
        if not file_path:
            self.report({"ERROR"}, "No file path selected.")
            return {"CANCELLED"}

        self._result = None

        def _bulk():
            self._result = install_packages_from_requirements(file_path)

        self._thread = threading.Thread(target=_bulk, daemon=True)
        self._thread.start()
        context.window_manager.modal_handler_add(self)
        self.report({"INFO"}, f"Installing packages from {file_path}...")
        return {"RUNNING_MODAL"}


class WM_OT_SearchInstalledPackages(bpy.types.Operator):
    """Filter the installed packages list by search query"""
    bl_idname = "wm.search_installed_packages"
    bl_label = "Search Installed Packages"

    def execute(self, context):
        search_query = context.scene.installed_search_query.lower()
        for item in context.scene.installed_package_list:
            item.hide = search_query not in item.name.lower()
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class PackageItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    version: bpy.props.StringProperty()
    author: bpy.props.StringProperty()
    auto_update: bpy.props.BoolProperty()
    hide: bpy.props.BoolProperty(default=False)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


classes = (
    PackageItem,
    PackageManagementPanel,
    WM_OT_SearchPackages,
    WM_OT_RefreshInstalledPackages,
    WM_OT_DownloadPackage,
    WM_OT_DisablePackage,
    WM_OT_BulkDownloadPackages,
    WM_OT_FileSelect,
    WM_OT_SearchInstalledPackages,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.search_query = bpy.props.StringProperty(
        name="Search Query"
    )
    bpy.types.Scene.bulk_download_path = bpy.props.StringProperty(
        name="Bulk Download Path"
    )
    bpy.types.Scene.package_list = bpy.props.CollectionProperty(
        type=PackageItem
    )
    bpy.types.Scene.installed_package_list = bpy.props.CollectionProperty(
        type=PackageItem
    )
    bpy.types.Scene.show_search_results = bpy.props.BoolProperty(
        name="Show Search Results", default=True
    )
    bpy.types.Scene.show_installed_packages = bpy.props.BoolProperty(
        name="Show Installed Packages", default=True
    )
    bpy.types.Scene.installed_search_query = bpy.props.StringProperty(
        name="Search Installed Packages"
    )


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.search_query
    del bpy.types.Scene.bulk_download_path
    del bpy.types.Scene.package_list
    del bpy.types.Scene.installed_package_list
    del bpy.types.Scene.show_search_results
    del bpy.types.Scene.show_installed_packages
    del bpy.types.Scene.installed_search_query


if __name__ == "__main__":
    if hasattr(bpy.utils, "register_class"):
        register()
