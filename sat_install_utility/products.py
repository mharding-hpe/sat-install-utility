"""
Contains the ProductCatalog and InstalledProductVersion classes.

(C) Copyright 2021 Hewlett Packard Enterprise Development LP.

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
"""

import os
import subprocess

from kubernetes.client.rest import ApiException
from urllib3.exceptions import MaxRetryError
from urllib.error import HTTPError
from yaml import safe_load, YAMLError


COMPONENT_VERSIONS_PRODUCT_MAP_KEY = 'component_versions'


class ProductInstallException(Exception):
    """An error occurred reading or manipulating product installs."""
    pass


class ProductCatalog:
    """A collection of installed product versions.

    Attributes:
        products ([InstalledProductVersion]): A list of installed product
            versions.
    """

    def __init__(self, name, namespace, k8s_api):
        """Create the ProductCatalog object.

        Args:
            name (str): The name of the product catalog Kubernetes config map.
            namespace (str): The namespace of the product catalog Kubernetes
                config map.
            k8s_api (CoreV1Api): The Kubernetes API for reading the config map.

        Raises:
            ProductInstallException: if reading the config map failed.
        """
        try:
            config_map = k8s_api.read_namespaced_config_map(name, namespace)
        except MaxRetryError as err:
            raise ProductInstallException(
                f'Unable to connect to Kubernetes to read {namespace}/{name} ConfigMap: {err}'
            )
        except ApiException as err:
            # The full string representation of ApiException is very long, so just log err.reason.
            raise ProductInstallException(
                f'Error reading {namespace}/{name} ConfigMap: {err.reason}'
            )

        if config_map.data is None:
            raise ProductInstallException(
                f'No data found in {namespace}/{name} ConfigMap.'
            )

        try:
            self.products = [
                InstalledProductVersion(product_name, product_version, product_version_data)
                for product_name, product_versions in config_map.data.items()
                for product_version, product_version_data in safe_load(product_versions).items()
            ]
        except YAMLError as err:
            raise ProductInstallException(
                f'Failed to load ConfigMap data: {err}'
            )

    def get_matching_products(self, name, version):
        """Get the InstalledProductVersion matching the given name/version.

        Args:
            name (str): The product name.
            version (str): The product version.

        Returns:
            tuple: A 2-tuple, where the first element is the matching
                InstalledProductVersion and the second is a list of all
                other InstalledProductVersion objects.

        Raises:
            ProductInstallException: If there is more than one matching
                InstalledProductVersion, or if there are none.
        """
        matching_products = [
            product for product in self.products
            if product.name == name and product.version == version
        ]
        other_products = [
            product for product in self.products
            if product.name != name or product.version != version
        ]
        if not matching_products:
            raise ProductInstallException(
                f'No installed products with name {name} and version {version}.'
            )
        elif len(matching_products) > 1:
            raise ProductInstallException(
                f'Multiple installed products with name {name} and version {version}.'
            )

        return matching_products[0], other_products


class InstalledProductVersion:
    """A representation of a version of a product that is currently installed.

    Attributes:
        name: The product name.
        version: The product version.
        data: A dictionary representing the data within a given product and
              version in the product catalog, which is expected to contain a
              'component_versions' key that will point to the respective
              versions of product components, e.g. Docker images.
    """

    def __init__(self, name, version, data):
        self.name = name
        self.version = version
        self.data = data

    def __str__(self):
        return f'{self.name}-{self.version}'

    @property
    def docker_images(self):
        """Get Docker images associated with this InstalledProductVersion.

        Returns:
            A dictionary whose keys are Docker image names and whose values are
                docker image versions.
        """
        component_data = self.data.get(COMPONENT_VERSIONS_PRODUCT_MAP_KEY, {})

        # If there is no 'docker' key under the component data, assume that there
        # is a single docker image named cray/cray-PRODUCT whose version is the
        # value of the PRODUCT key under component_versions.
        if 'docker' not in component_data:
            return {self._deprecated_docker_image_name: self._deprecated_docker_image_version}

        return component_data['docker'] or {}

    @property
    def _deprecated_docker_image_version(self):
        """str: The Docker image version associated with this product version, or None.

        Note: this assumes that the 'component_versions' data is structured as follows:
        component_versions:
            product_name: docker_image_version

        Newer versions will structure the 'component_versions' data as follows:
        component_versions:
            product_name:
                docker:
                    docker_image_name_1: docker_image_version
                    docker_image_name_2: docker_image_version

        This method should only be used if the installed version does not have a
        component_versions->product_name->docker key.
        """
        return self.data.get(COMPONENT_VERSIONS_PRODUCT_MAP_KEY, {}).get(self.name)

    @property
    def _deprecated_docker_image_name(self):
        """str: The Docker image name associated with this product version.

        Note: this assumes that the 'component_versions' data is structured as follows:
        component_versions:
            product_name: docker_image_version

        It also assumes that the name of the singular docker image is
        'cray/cray-<product_name'.

        Newer versions will structure the 'component_versions' data as follows:
        component_versions:
            product_name:
                docker:
                    docker_image_name_1: docker_image_version
                    docker_image_name_2: docker_image_version

        This method should only be used if the installed version does not have a
        component_versions->product_name->docker key.
        """
        return f'cray/cray-{self.name}'

    def get_group_repo_name(self, dist):
        """Get the name of this product's 'group' repository, i.e. NAME-DIST

        Args:
            dist (str): The name of the distribution associated with the group
                repository.

        Returns:
            str: The group repository name.
        """
        return f'{self.name}-{dist}'

    def get_hosted_repo_name(self, dist):
        """Get the name of the hosted repository, i.e. NAME-VERSION-DIST.

        Args:
            dist (str): The name of the distribution associated with the hosted
                repository.

        Returns:
            str: The hosted repository name.

        """
        return f'{self.name}-{self.version}-{dist}'

    def uninstall_hosted_repo(self, nexus_api, dist):
        """Uninstall a version by removing its package repository from Nexus.

        Args:
            nexus_api (NexusApi): The nexusctl Nexus API to interface with
                Nexus.
            dist (str): The name of the distribution associated with the hosted
                repository.

        Returns:
            None

        Raises:
            ProductInstallException: If an error occurred removing the repository.
        """
        hosted_repo_name = self.get_hosted_repo_name(dist)
        try:
            nexus_api.repos.delete(hosted_repo_name)
            print(f'Repository {hosted_repo_name} has been removed.')
        except HTTPError as err:
            if err.code == 404:
                print(f'{hosted_repo_name} has already been removed.')
            else:
                raise ProductInstallException(
                    f'Failed to remove repository {hosted_repo_name}: {err}'
                )

    @staticmethod
    def uninstall_docker_image(docker_image_name, docker_image_version, docker_api):
        """Remove the Docker image associated with this product version.

        Args:
            docker_image_name (str): The name of the Docker image to uninstall.
            docker_image_version (str): The version of the Docker image to uninstall.
            docker_api (DockerApi): The nexusctl Docker API to interface with
                the Docker registry.

        Returns:
            None

        Raises:
            ProductInstallException: If an error occurred removing the image.
        """
        # TODO: can we do something cleaner than taking the name/version as params?
        docker_image_short_name = f'{docker_image_name}:{docker_image_version}'
        try:
            docker_api.delete_image(
                docker_image_name, docker_image_version
            )
            print(f'Removed Docker image {docker_image_short_name}')
        except HTTPError as err:
            if err.code == 404:
                print(f'{docker_image_short_name} has already been removed.')
            else:
                raise ProductInstallException(
                    f'Failed to remove image {docker_image_short_name}: {err}'
                )

    def remove_from_product_catalog(self, config_map_name, config_map_namespace):
        """Remove this product version's entry from the product catalog.

        This function uses the catalog_delete.py script provided by
        cray-product-catalog.

        Args:
            config_map_name (str): The name of the product catalog config map.
            config_map_namespace (str): The namespace of the product catalog
                config map.

        Returns:
            None

        Raises:
            ProductInstallException: If an error occurred removing the entry.
        """
        # Use os.environ so that PATH and VIRTUAL_ENV are used
        os.environ.update({
            'PRODUCT': self.name,
            'PRODUCT_VERSION': self.version,
            'CONFIG_MAP': config_map_name,
            'CONFIG_MAP_NS': config_map_namespace
        })
        try:
            subprocess.check_output(['catalog_delete.py'])
            print(f'Deleted {self.name}-{self.version} from product catalog.')
        except subprocess.CalledProcessError as err:
            raise ProductInstallException(
                f'Error removing {self.name}-{self.version} from product catalog: {err}'
            )

    @staticmethod
    def _get_repo_by_name(nexus_api, name):
        """Get a repository with the specified name.

        Args:
            nexus_api (NexusApi): The nexusctl Nexus API to interface with
                Nexus.
            name (str): The name of the repository.

        Returns:
            RepoListHostedEntry: If the repository is a hosted repository
            RepoListGroupEntry: If the repository is a group repository

        Raises:
            ProductInstallException: if more than one repository with the specified name
                is found, or if none are found with the specified name.
            ProductInstallException: if an API error occurs.
        """
        try:
            repos_matching_name = nexus_api.repos.list(regex=f'^{name}$')
            if len(repos_matching_name) > 1:
                raise ProductInstallException(f'More than one repository named {name} found.')
            return repos_matching_name[0]
        except IndexError:
            raise ProductInstallException(f'No repository named {name} found.')
        except HTTPError as err:
            raise ProductInstallException(f'Failed to get repository {name}: {err}')

    def activate_hosted_repo(self, nexus_api, dist):
        """Activate a version by making its hosted repository the default.

        This uses the Nexus API to make a hosted-type repo the first entry in a
        group-type repo.

        Args:
            nexus_api (NexusApi): The nexusctl Nexus API to interface with
                Nexus.
            dist (str): The name of the distribution associated with the hosted
                and group repositories.

        Returns:
            None

        Raises:
            ProductInstallException: if an error occurred activating the hosted
                repository.
        """
        hosted_repo_name = self.get_hosted_repo_name(dist)
        group_repo_name = self.get_group_repo_name(dist)
        # Ensure hosted repo exists
        try:
            self._get_repo_by_name(nexus_api, hosted_repo_name)
        except ProductInstallException as err:
            raise ProductInstallException(
                f'Unable to identify hosted repository for version {self.version} of {self.name}: {err}'
            )
        try:
            group_repo = self._get_repo_by_name(nexus_api, group_repo_name)
        except ProductInstallException as err:
            raise ProductInstallException(
                f'Unable to identify group repository for version {self.version} of {self.name}: {err}'
            )
        # Put hosted repo first in the list, making it 'active'.
        members = [
            str(name)
            for name in group_repo.group.member_names
            if name != hosted_repo_name
        ]
        members.insert(0, hosted_repo_name)

        try:
            nexus_api.repos.raw_group.update(
                group_repo.name,
                group_repo.online,
                group_repo.storage.blobstore_name,
                group_repo.storage.strict_content_type_validation,
                member_names=tuple(members)
            )
            print(f'Repository {hosted_repo_name} is now the default in {group_repo_name}.')
        except HTTPError as err:
            raise ProductInstallException(
                f'Failed to activate {hosted_repo_name} in {group_repo_name}: {err}'
            )
