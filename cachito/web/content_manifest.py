# SPDX-License-Identifier: GPL-3.0-or-later
import flask

from cachito.web.utils import deep_sort_icm


class ContentManifest:
    """A content manifest associated with a Cacihto request."""

    version = 1
    json_schema_url = (
        "https://raw.githubusercontent.com/containerbuildsystem/atomic-reactor/"
        "f4abcfdaf8247a6b074f94fa84f3846f82d781c6/atomic_reactor/schemas/content_manifest.json"
    )
    unknown_layer_index = -1

    def __init__(self, request):
        """
        Initialize ContentManifest.

        :param Request request: the request to generate a ContentManifest for
        """
        self.request = request
        # dict to store go package level data; uses the package purl as key to identify a package
        self._gopkg_data = {}
        # dict to store go module level purl dependencies. Module names are used as keys
        self._gomod_data = {}
        # dict to store npm package data; uses the package purl as key to identify a package
        self._npm_data = {}
        # dict to store pip package data; uses the package purl as key to identify a package
        self._pip_data = {}
        # dict to store gitsubmodule package level data; uses the package purl as key to identify a
        # package
        self._gitsubmodule_data = {}

    def process_gomod(self, package, dependency):
        """
        Process gomod package.

        :param Package package: the gomod package to process
        :param Dependency dependency: the gomod package dependency to process
        """
        if dependency.type == "gomod":
            icm_source = {"purl": dependency.to_purl()}
            self._gomod_data[package.name].append(icm_source)

    def process_go_package(self, package, dependency):
        """
        Process go-package package.

        :param Package package: the go-package package to process
        :param Dependency dependency: the go-package package dependency to process
        """
        if dependency.type == "go-package":
            icm_dependency = {"purl": dependency.to_purl()}
            self._gopkg_data[package.id]["dependencies"].append(icm_dependency)

    def set_go_package_sources(self):
        """
        Adjust source level dependencies for go packages.

        Go packages are not related to Go modules in cachito's DB. However, Go
        sources are retreived in a per module basis. To set the proper source
        in each content manifest entry, we associate each Go package to a Go
        module based on their names.
        """
        for package_id, pkg_data in self._gopkg_data.items():
            if pkg_data["name"] in self._gomod_data:
                self._gopkg_data[package_id]["sources"] = self._gomod_data[pkg_data["name"]]
            else:
                # We use the longest module available in the request that matches the package name
                previous_length = 0
                for mod_name, sources in self._gomod_data.items():
                    if pkg_data["name"].startswith(mod_name) and len(mod_name) > previous_length:
                        self._gopkg_data[package_id]["sources"] = sources
                        previous_length = len(mod_name)

                if not previous_length:
                    flask.current_app.logger.warning(
                        "Could not find a Go module for %s", pkg_data["purl"]
                    )
            pkg_data.pop("name")

    def process_npm_package(self, package, dependency):
        """
        Process npm package.

        :param Package package: the npm package to process
        :param Dependency dependency: the npm package dependency to process
        """
        if dependency.type == "npm":
            self._process_standard_package("npm", package, dependency)

    def process_pip_package(self, package, dependency):
        """
        Process pip package.

        :param Package package: the pip package to process
        :param Dependency dependency: the pip package dependency to process
        """
        if dependency.type == "pip":
            self._process_standard_package("pip", package, dependency)

    def _process_standard_package(self, pkg_type, package, dependency):
        """
        Process a standard package (standard = does not require the same magic as go packages).

        Currently, all package types except for gomod and go-package are standard.
        """
        pkg_type_data = getattr(self, f"_{pkg_type}_data")

        icm_dependency = {"purl": dependency.to_purl()}
        pkg_type_data[package.id]["sources"].append(icm_dependency)
        if not dependency.dev:
            pkg_type_data[package.id]["dependencies"].append(icm_dependency)

    def to_json(self):
        """
        Generate the JSON representation of the content manifest.

        :return: the JSON form of the ContentManifest object
        :rtype: OrderedDict
        """
        self._gopkg_data = {}
        self._gomod_data = {}
        self._npm_data = {}
        self._pip_data = {}
        self._gitsubmodule_data = {}

        # Address the possibility of packages having no dependencies
        for request_package in self.request.request_packages:
            package = request_package.package

            if package.type == "go-package":
                purl = package.to_top_level_purl(self.request, subpath=request_package.subpath)
                self._gopkg_data.setdefault(
                    package.id,
                    {"name": package.name, "purl": purl, "dependencies": [], "sources": []},
                )
            elif package.type == "gomod":
                self._gomod_data.setdefault(package.name, [])
            elif package.type in ("npm", "pip"):
                purl = package.to_top_level_purl(self.request, subpath=request_package.subpath)
                data = getattr(self, f"_{package.type}_data")
                data.setdefault(package.id, {"purl": purl, "dependencies": [], "sources": []})
            elif package.type == "git-submodule":
                purl = package.to_top_level_purl(self.request, subpath=request_package.subpath)
                self._gitsubmodule_data.setdefault(
                    package.id, {"purl": purl, "dependencies": [], "sources": []}
                )
            else:
                flask.current_app.logger.debug(
                    "No ICM implementation for '%s' packages", package.type
                )

        for req_dep in self.request.request_dependencies:
            if req_dep.package.type == "go-package":
                self.process_go_package(req_dep.package, req_dep.dependency)
            elif req_dep.package.type == "gomod":
                self.process_gomod(req_dep.package, req_dep.dependency)
            elif req_dep.package.type == "npm":
                self.process_npm_package(req_dep.package, req_dep.dependency)
            elif req_dep.package.type == "pip":
                self.process_pip_package(req_dep.package, req_dep.dependency)

        # Adjust source level dependencies for go packages
        self.set_go_package_sources()

        top_level_packages = [
            *self._gopkg_data.values(),
            *self._npm_data.values(),
            *self._pip_data.values(),
            *self._gitsubmodule_data.values(),
        ]
        return self.generate_icm(top_level_packages)

    def generate_icm(self, image_contents=None):
        """
        Generate a content manifest with the given image contents.

        :param list image_contents: List with components for the ICM's ``image_contents`` field
        :return: a valid Image Content Manifest
        :rtype: OrderedDict
        """
        icm = {
            "metadata": {
                "icm_version": self.version,
                "icm_spec": self.json_schema_url,
                "image_layer_index": self.unknown_layer_index,
            },
        }
        icm["image_contents"] = image_contents or []

        return deep_sort_icm(icm)
