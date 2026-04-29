# Copyright (C) 2026 Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from unittest import mock

import fixtures
from openstack import exceptions as os_exceptions

from ceilometer import service
from ceilometer.tests import base
from ceilometer.tests.unit import fakes
from ceilometer.volume import discovery


class _BaseDiscoveryTestCase(base.BaseTestCase):

    def setUp(self):
        super().setUp()
        self.CONF = service.prepare_service([], [])
        self.useFixture(
            fixtures.MockPatch('ceilometer.keystone_client.get_session'))
        self.manager = mock.Mock()


class TestVolumeDiscovery(_BaseDiscoveryTestCase):

    def setUp(self):
        super().setUp()
        self.discovery = discovery.VolumeDiscovery(self.CONF)

    def test_keystone_required_for_cinder_service(self):
        self.assertEqual(
            'cinder',
            self.discovery.KEYSTONE_REQUIRED_FOR_SERVICE)

    def test_discover_returns_volumes(self):
        resources = self.discovery.discover(self.manager)

        self.assertEqual(fakes.VOLUME_LIST, resources)

    def test_discover_empty(self):
        self.setup_connection(volumes=[])
        self.discovery = discovery.VolumeDiscovery(self.CONF)

        resources = self.discovery.discover(self.manager)

        self.assertEqual([], resources)

    def test_discover_calls_list_volumes_with_all_projects(self):
        result = self.discovery.discover(self.manager)

        self.fake_conn.block_storage.volumes.assert_called_once_with(
            details=True, all_projects=True)
        self.assertEqual(fakes.VOLUME_LIST, result)

    def test_discover_propagates_exception(self):
        self.fake_conn.block_storage.volumes = mock.Mock(
            side_effect=os_exceptions.HttpException())

        self.assertRaises(
            os_exceptions.HttpException,
            self.discovery.discover, self.manager)


class TestVolumeSnapshotsDiscovery(_BaseDiscoveryTestCase):

    def setUp(self):
        super().setUp()
        self.discovery = discovery.VolumeSnapshotsDiscovery(self.CONF)

    def test_keystone_required_for_cinder_service(self):
        self.assertEqual(
            'cinder',
            self.discovery.KEYSTONE_REQUIRED_FOR_SERVICE)

    def test_discover_returns_snapshots(self):
        resources = self.discovery.discover(self.manager)

        self.assertEqual(fakes.SNAPSHOT_LIST, resources)

    def test_discover_empty(self):
        self.setup_connection(snapshots=[])
        self.discovery = discovery.VolumeSnapshotsDiscovery(self.CONF)

        resources = self.discovery.discover(self.manager)

        self.assertEqual([], resources)

    def test_discover_calls_list_volume_snapshots_with_all_projects(self):
        result = self.discovery.discover(self.manager)

        self.fake_conn.block_storage.snapshots.assert_called_once_with(
            details=True, all_projects=True)
        self.assertEqual(fakes.SNAPSHOT_LIST, result)

    def test_discover_propagates_exception(self):
        self.fake_conn.block_storage.snapshots = mock.Mock(
            side_effect=os_exceptions.HttpException())

        self.assertRaises(
            os_exceptions.HttpException,
            self.discovery.discover,
            self.manager)


class TestVolumeBackupsDiscovery(_BaseDiscoveryTestCase):

    def setUp(self):
        super().setUp()
        self.discovery = discovery.VolumeBackupsDiscovery(self.CONF)

    def test_keystone_required_for_cinder_service(self):
        self.assertEqual(
            'cinder',
            self.discovery.KEYSTONE_REQUIRED_FOR_SERVICE)

    def test_discover_returns_backups(self):
        resources = self.discovery.discover(self.manager)

        self.assertEqual(fakes.BACKUP_LIST, resources)

    def test_discover_empty(self):
        self.setup_connection(backups=[])
        self.discovery = discovery.VolumeBackupsDiscovery(self.CONF)

        resources = self.discovery.discover(self.manager)

        self.assertEqual([], resources)

    def test_discover_calls_list_backups_with_all_projects(self):
        result = self.discovery.discover(self.manager)

        self.fake_conn.block_storage.backups.assert_called_once_with(
            details=True, all_projects=True)
        self.assertEqual(fakes.BACKUP_LIST, result)

    def test_discover_propagates_exception(self):
        self.fake_conn.block_storage.backups = mock.Mock(
            side_effect=os_exceptions.HttpException())

        self.assertRaises(
            os_exceptions.HttpException,
            self.discovery.discover,
            self.manager)


class TestVolumePoolsDiscovery(_BaseDiscoveryTestCase):

    def setUp(self):
        super().setUp()
        self.discovery = discovery.VolumePoolsDiscovery(self.CONF)

    def test_keystone_required_for_cinder_service(self):
        self.assertEqual(
            'cinder',
            self.discovery.KEYSTONE_REQUIRED_FOR_SERVICE)

    def test_discover_returns_pools(self):
        resources = self.discovery.discover(self.manager)

        self.assertEqual(fakes.POOL_LIST, resources)

    def test_discover_empty(self):
        self.setup_connection(pools=[])
        self.discovery = discovery.VolumePoolsDiscovery(self.CONF)

        resources = self.discovery.discover(self.manager)

        self.assertEqual([], resources)

    def test_discover_calls_backend_pools(self):
        self.discovery.discover(self.manager)

        self.fake_conn.block_storage.backend_pools.assert_called_once_with()

    def test_discover_propagates_exception(self):
        self.fake_conn.block_storage.backend_pools = mock.Mock(
            side_effect=os_exceptions.HttpException())

        self.assertRaises(
            os_exceptions.HttpException,
            self.discovery.discover,
            self.manager)


class TestVolumeServicesDiscovery(_BaseDiscoveryTestCase):

    def setUp(self):
        super().setUp()
        self.discovery = discovery.VolumeServicesDiscovery(self.CONF)

    def test_keystone_required_for_cinder_service(self):
        self.assertEqual(
            'cinder',
            self.discovery.KEYSTONE_REQUIRED_FOR_SERVICE)

    def test_discover_returns_services(self):
        resources = self.discovery.discover(self.manager)

        self.assertEqual(fakes.SERVICE_LIST, resources)

    def test_discover_empty(self):
        self.setup_connection(services=[])
        self.discovery = discovery.VolumeServicesDiscovery(self.CONF)

        resources = self.discovery.discover(self.manager)

        self.assertEqual([], resources)

    def test_discover_calls_list_with_no_args(self):
        result = self.discovery.discover(self.manager)

        self.fake_conn.block_storage.services.assert_called_once_with()
        self.assertEqual(fakes.SERVICE_LIST, result)

    def test_discover_propagates_exception(self):
        self.fake_conn.block_storage.services = mock.Mock(
            side_effect=os_exceptions.HttpException())

        self.assertRaises(
            os_exceptions.HttpException,
            self.discovery.discover,
            self.manager)
