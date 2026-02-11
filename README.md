# Package Manager

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=kents00_Package-Manager&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=kents00_Package-Manager)
[![Semgrep](https://github.com/kents00/Package-Manager/actions/workflows/semgrep.yml/badge.svg)](https://github.com/kents00/Package-Manager/actions/workflows/semgrep.yml)
[![DeepSource](https://app.deepsource.com/gh/kents00/Package-Manager.svg/?label=active+issues&show_trend=true)](https://app.deepsource.com/gh/kents00/Package-Manager/)

The Package Manager is an add-on designed to streamline the management of Python packages directly within Blender. It provides an easy-to-use interface for searching, installing, and managing Python packages, along with bulk download capabilities for package requirements.

This add-on aims to simplify the workflow for Blender users who need to manage Python packages for scripting and plugin development. It eliminates the need to use external package managers by integrating package management into the Blender interface.

![Package Installer 1](https://github.com/user-attachments/assets/d0238775-e577-478e-a37f-ff95e1290e16)

## Features

- **Search PyPI** — Find Python packages with cached results and rate-limited requests
- **Install packages** — Download and install packages without freezing Blender's UI
- **Manage installed packages** — View, search, and uninstall packages
- **Bulk download** — Install all packages from a `requirements.txt` file in a single pip call
- **Non-blocking UI** — Network and subprocess operations run in background threads

## Usage

### 1. **Search Packages**

- **Search Packages:** Use the "Search Packages" section to find Python packages available on PyPI (Python Package Index). Enter your search query and click "Search" to view results.
- **Install Packages:** From the search results, you can install packages by clicking the "Download" button next to each package.

### 2. **Manage Installed Packages**

- **Refresh Installed Packages:** Click "Refresh" to update the list of installed packages in Blender.
- **Search Installed Packages:** Use the "Search Installed Packages" field to filter the list of installed packages.
- **Disable Packages:** You can uninstall packages by clicking the "Disable" button next to each package. The package list will automatically update to reflect these changes.

### 3. **Bulk Download Packages**

- **Choose File Path:** Use the "Choose File Path" button to select a requirements file containing a list of packages to install.
- **Download All:** Click "Download All" to install all packages listed in the selected requirements file.

## Installation

### As a Blender 4.2+ Extension

1. **Download** the extension as a `.zip` file from this repository.
2. Open Blender and go to **Edit > Preferences > Get Extensions**.
3. Click the dropdown arrow (⌄) next to "Repositories" and select **Install from Disk...**.
4. Select the `.zip` file.
5. Enable the extension.

### Accessing the Panel

- Switch to the **Text Editor** workspace.
- Open the sidebar (press `N` or drag from the right edge).
- You will find the **Package Manager** panel in the **Package Manager** tab.

## Notes

- The add-on requires **Blender 4.2** or higher.
- Internet access is required for searching and downloading packages.
- Once a package is installed or removed, a restart may be required to apply changes.
- Search results are cached for 5 minutes to reduce network requests.
- A 2-second cooldown is applied between searches to prevent excessive API calls.

## Contributing

Feel free to open issues or submit pull requests if you find bugs or have suggestions for improvements. Contributions are welcome!
