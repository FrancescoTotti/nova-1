# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 Hewlett-Packard Development Company, L.P.
# Copyright (c) 2012 VMware, Inc.
# Copyright (c) 2011 Citrix Systems, Inc.
# Copyright 2011 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Test suite for VMwareAPI.
"""

import mox
from oslo.config import cfg

from nova import block_device
from nova.compute import api as compute_api
from nova.compute import power_state
from nova.compute import task_states
from nova import context
from nova import db
from nova import exception
from nova import test
import nova.tests.image.fake
from nova.tests import matchers
from nova.tests import utils
from nova.tests.virt.vmwareapi import db_fakes
from nova.tests.virt.vmwareapi import stubs
from nova.virt import driver as v_driver
from nova.virt import fake
from nova.virt.vmwareapi import driver
from nova.virt.vmwareapi import fake as vmwareapi_fake
from nova.virt.vmwareapi import vim
from nova.virt.vmwareapi import vm_util
from nova.virt.vmwareapi import vmops
from nova.virt.vmwareapi import volume_util
from nova.virt.vmwareapi import volumeops


class fake_vm_ref(object):
    def __init__(self):
        self.value = 4
        self._type = 'VirtualMachine'


class VMwareAPIConfTestCase(test.TestCase):
    """Unit tests for VMWare API configurations."""
    def setUp(self):
        super(VMwareAPIConfTestCase, self).setUp()

    def tearDown(self):
        super(VMwareAPIConfTestCase, self).tearDown()

    def test_configure_without_wsdl_loc_override(self):
        # Test the default configuration behavior. By default,
        # use the WSDL sitting on the host we are talking to in
        # order to bind the SOAP client.
        wsdl_loc = cfg.CONF.vmware.wsdl_location
        self.assertIsNone(wsdl_loc)
        wsdl_url = vim.Vim.get_wsdl_url("https", "www.example.com")
        url = vim.Vim.get_soap_url("https", "www.example.com")
        self.assertEqual("https://www.example.com/sdk/vimService.wsdl",
                         wsdl_url)
        self.assertEqual("https://www.example.com/sdk", url)

    def test_configure_without_wsdl_loc_override_using_ipv6(self):
        # Same as above but with ipv6 based host ip
        wsdl_loc = cfg.CONF.vmware.wsdl_location
        self.assertIsNone(wsdl_loc)
        wsdl_url = vim.Vim.get_wsdl_url("https", "::1")
        url = vim.Vim.get_soap_url("https", "::1")
        self.assertEqual("https://[::1]/sdk/vimService.wsdl",
                         wsdl_url)
        self.assertEqual("https://[::1]/sdk", url)

    def test_configure_with_wsdl_loc_override(self):
        # Use the setting vmwareapi_wsdl_loc to override the
        # default path to the WSDL.
        #
        # This is useful as a work-around for XML parsing issues
        # found when using some WSDL in combination with some XML
        # parsers.
        #
        # The wsdl_url should point to a different host than the one we
        # are actually going to send commands to.
        fake_wsdl = "https://www.test.com/sdk/foo.wsdl"
        self.flags(wsdl_location=fake_wsdl, group='vmware')
        wsdl_loc = cfg.CONF.vmware.wsdl_location
        self.assertIsNotNone(wsdl_loc)
        self.assertEqual(fake_wsdl, wsdl_loc)
        wsdl_url = vim.Vim.get_wsdl_url("https", "www.example.com")
        url = vim.Vim.get_soap_url("https", "www.example.com")
        self.assertEqual(fake_wsdl, wsdl_url)
        self.assertEqual("https://www.example.com/sdk", url)


class VMwareAPIVMTestCase(test.TestCase):
    """Unit tests for Vmware API connection calls."""

    def setUp(self):
        super(VMwareAPIVMTestCase, self).setUp()
        self.context = context.RequestContext('fake', 'fake', is_admin=False)
        self.flags(host_ip='test_url',
                   host_username='test_username',
                   host_password='test_pass',
                   use_linked_clone=False, group='vmware')
        self.flags(vnc_enabled=False)
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.node_name = 'test_url'
        self.context = context.RequestContext(self.user_id, self.project_id)
        vmwareapi_fake.reset()
        db_fakes.stub_out_db_instance_api(self.stubs)
        stubs.set_stubs(self.stubs)
        self.conn = driver.VMwareESXDriver(fake.FakeVirtAPI)
        # NOTE(vish): none of the network plugging code is actually
        #             being tested
        self.network_info = utils.get_test_network_info()

        self.image = {
            'id': 'c1c8ce3d-c2e0-4247-890c-ccf5cc1c004c',
            'disk_format': 'vhd',
            'size': 512,
        }
        nova.tests.image.fake.stub_out_image_service(self.stubs)

    def tearDown(self):
        super(VMwareAPIVMTestCase, self).tearDown()
        vmwareapi_fake.cleanup()
        nova.tests.image.fake.FakeImageService_reset()

    def _create_instance_in_the_db(self, node=None):
        if not node:
            node = self.node_name
        values = {'name': 'fake_name',
                  'id': 1,
                  'uuid': "fake-uuid",
                  'project_id': self.project_id,
                  'user_id': self.user_id,
                  'image_ref': "fake_image_uuid",
                  'kernel_id': "fake_kernel_uuid",
                  'ramdisk_id': "fake_ramdisk_uuid",
                  'mac_address': "de:ad:be:ef:be:ef",
                  'instance_type': 'm1.large',
                  'node': node,
                  }
        self.instance = db.instance_create(None, values)

    def _create_vm(self, node=None, num_instances=1):
        """Create and spawn the VM."""
        if not node:
            node = self.node_name
        self._create_instance_in_the_db(node=node)
        self.type_data = db.flavor_get_by_name(None, 'm1.large')
        self.conn.spawn(self.context, self.instance, self.image,
                        injected_files=[], admin_password=None,
                        network_info=self.network_info,
                        block_device_info=None)
        self._check_vm_record(num_instances=num_instances)

    def _check_vm_record(self, num_instances=1):
        """
        Check if the spawned VM's properties correspond to the instance in
        the db.
        """
        instances = self.conn.list_instances()
        self.assertEquals(len(instances), num_instances)

        # Get Nova record for VM
        vm_info = self.conn.get_info({'uuid': 'fake-uuid',
                                      'name': 1})

        # Get record for VM
        vms = vmwareapi_fake._get_objects("VirtualMachine")
        vm = vms.objects[0]

        # Check that m1.large above turned into the right thing.
        mem_kib = long(self.type_data['memory_mb']) << 10
        vcpus = self.type_data['vcpus']
        self.assertEquals(vm_info['max_mem'], mem_kib)
        self.assertEquals(vm_info['mem'], mem_kib)
        self.assertEquals(vm.get("summary.config.numCpu"), vcpus)
        self.assertEquals(vm.get("summary.config.memorySizeMB"),
                          self.type_data['memory_mb'])

        self.assertEqual(
            vm.get("config.hardware.device")[2].device.obj_name,
            "ns0:VirtualE1000")
        # Check that the VM is running according to Nova
        self.assertEquals(vm_info['state'], power_state.RUNNING)

        # Check that the VM is running according to vSphere API.
        self.assertEquals(vm.get("runtime.powerState"), 'poweredOn')

        found_vm_uuid = False
        found_iface_id = False
        for c in vm.get("config.extraConfig"):
            if (c.key == "nvp.vm-uuid" and c.value == self.instance['uuid']):
                found_vm_uuid = True
            if (c.key == "nvp.iface-id.0" and c.value == "vif-xxx-yyy-zzz"):
                found_iface_id = True

        self.assertTrue(found_vm_uuid)
        self.assertTrue(found_iface_id)

    def _check_vm_info(self, info, pwr_state=power_state.RUNNING):
        """
        Check if the get_info returned values correspond to the instance
        object in the db.
        """
        mem_kib = long(self.type_data['memory_mb']) << 10
        self.assertEquals(info["state"], pwr_state)
        self.assertEquals(info["max_mem"], mem_kib)
        self.assertEquals(info["mem"], mem_kib)
        self.assertEquals(info["num_cpu"], self.type_data['vcpus'])

    def test_list_instances(self):
        instances = self.conn.list_instances()
        self.assertEquals(len(instances), 0)

    def test_list_instances_1(self):
        self._create_vm()
        instances = self.conn.list_instances()
        self.assertEquals(len(instances), 1)

    def test_instance_dir_disk_created(self):
        """Test image file isn't cached when use_linked_clone is False."""
        self._create_vm()
        inst_file_path = '[fake-ds] fake-uuid/fake_name.vmdk'
        cache_file_path = '[fake-ds] vmware_base/fake_image_uuid.vmdk'
        self.assertEqual(vmwareapi_fake.get_file(inst_file_path), True)
        self.assertEqual(vmwareapi_fake.get_file(cache_file_path), False)

    def test_cache_dir_disk_created(self):
        """Test image disk is cached when use_linked_clone is True."""
        self.flags(use_linked_clone=True, group='vmware')
        self._create_vm()
        file_path = '[fake-ds] vmware_base/fake_image_uuid.vmdk'
        self.assertEqual(vmwareapi_fake.get_file(file_path), True)

    def test_spawn(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_spawn_attach_volume_vmdk(self):
        self._create_instance_in_the_db()
        self.type_data = db.flavor_get_by_name(None, 'm1.large')
        self.mox.StubOutWithMock(block_device, 'volume_in_mapping')
        self.mox.StubOutWithMock(v_driver, 'block_device_info_get_mapping')
        ebs_root = 'fake_root'
        block_device.volume_in_mapping(mox.IgnoreArg(),
                mox.IgnoreArg()).AndReturn(ebs_root)
        connection_info = self._test_vmdk_connection_info('vmdk')
        root_disk = [{'connection_info': connection_info}]
        v_driver.block_device_info_get_mapping(
                mox.IgnoreArg()).AndReturn(root_disk)
        mount_point = '/dev/vdc'
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_get_volume_uuid')
        volumeops.VMwareVolumeOps._get_volume_uuid(mox.IgnoreArg(),
                'volume-fake-id').AndReturn('fake_disk_uuid')
        self.mox.StubOutWithMock(vm_util, 'get_vmdk_backed_disk_device')
        vm_util.get_vmdk_backed_disk_device(mox.IgnoreArg(),
                'fake_disk_uuid').AndReturn('fake_device')
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_consolidate_vmdk_volume')
        volumeops.VMwareVolumeOps._consolidate_vmdk_volume(self.instance,
                 mox.IgnoreArg(), 'fake_device', mox.IgnoreArg())
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'attach_volume')
        volumeops.VMwareVolumeOps.attach_volume(connection_info,
                self.instance, mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conn.spawn(self.context, self.instance, self.image,
                        injected_files=[], admin_password=None,
                        network_info=self.network_info,
                        block_device_info=None)

    def test_spawn_attach_volume_iscsi(self):
        self._create_instance_in_the_db()
        self.type_data = db.flavor_get_by_name(None, 'm1.large')
        self.mox.StubOutWithMock(block_device, 'volume_in_mapping')
        self.mox.StubOutWithMock(v_driver, 'block_device_info_get_mapping')
        ebs_root = 'fake_root'
        block_device.volume_in_mapping(mox.IgnoreArg(),
                mox.IgnoreArg()).AndReturn(ebs_root)
        connection_info = self._test_vmdk_connection_info('iscsi')
        root_disk = [{'connection_info': connection_info}]
        v_driver.block_device_info_get_mapping(
                mox.IgnoreArg()).AndReturn(root_disk)
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'attach_volume')
        volumeops.VMwareVolumeOps.attach_volume(connection_info,
                self.instance, mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conn.spawn(self.context, self.instance, self.image,
                        injected_files=[], admin_password=None,
                        network_info=self.network_info,
                        block_device_info=None)

    def test_snapshot(self):
        expected_calls = [
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_PENDING_UPLOAD}},
            {'args': (),
             'kwargs':
                 {'task_state': task_states.IMAGE_UPLOADING,
                  'expected_state': task_states.IMAGE_PENDING_UPLOAD}}]
        func_call_matcher = matchers.FunctionCallMatcher(expected_calls)
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.conn.snapshot(self.context, self.instance, "Test-Snapshot",
                           func_call_matcher.call)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.assertIsNone(func_call_matcher.match())

    def test_snapshot_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound, self.conn.snapshot,
                          self.context, self.instance, "Test-Snapshot",
                          lambda *args, **kwargs: None)

    def test_reboot(self):
        self._create_vm()
        info = self.conn.get_info({'name': 1, 'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        reboot_type = "SOFT"
        self.conn.reboot(self.context, self.instance, self.network_info,
                         reboot_type)
        info = self.conn.get_info({'name': 1, 'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_reboot_with_uuid(self):
        """Test fall back to use name when can't find by uuid."""
        self._create_vm()
        info = self.conn.get_info({'name': 'fake-uuid', 'uuid': 'wrong-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        reboot_type = "SOFT"
        self.conn.reboot(self.context, self.instance, self.network_info,
                         reboot_type)
        info = self.conn.get_info({'name': 'fake-uuid', 'uuid': 'wrong-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_reboot_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound, self.conn.reboot,
                          self.context, self.instance, self.network_info,
                          'SOFT')

    def test_poll_rebooting_instances(self):
        self.mox.StubOutWithMock(compute_api.API, 'reboot')
        compute_api.API.reboot(mox.IgnoreArg(), mox.IgnoreArg(),
                               mox.IgnoreArg())
        self.mox.ReplayAll()
        self._create_vm()
        instances = [self.instance]
        self.conn.poll_rebooting_instances(60, instances)

    def test_reboot_not_poweredon(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.conn.suspend(self.instance)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SUSPENDED)
        self.assertRaises(exception.InstanceRebootFailure, self.conn.reboot,
                          self.context, self.instance, self.network_info,
                          'SOFT')

    def test_suspend(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': "fake-uuid"})
        self._check_vm_info(info, power_state.RUNNING)
        self.conn.suspend(self.instance)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SUSPENDED)

    def test_suspend_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound, self.conn.suspend,
                          self.instance)

    def test_resume(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.conn.suspend(self.instance)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SUSPENDED)
        self.conn.resume(self.instance, self.network_info)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_resume_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound, self.conn.resume,
                          self.instance, self.network_info)

    def test_resume_not_suspended(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.assertRaises(exception.InstanceResumeFailure, self.conn.resume,
                          self.instance, self.network_info)

    def test_power_on(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.conn.power_off(self.instance)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SHUTDOWN)
        self.conn.power_on(self.context, self.instance, self.network_info)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_power_on_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound, self.conn.power_on,
                          self.context, self.instance, self.network_info)

    def test_power_off(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self.conn.power_off(self.instance)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SHUTDOWN)

    def test_power_off_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound, self.conn.power_off,
                          self.instance)

    def test_power_off_suspended(self):
        self._create_vm()
        self.conn.suspend(self.instance)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SUSPENDED)
        self.assertRaises(exception.InstancePowerOffFailure,
                          self.conn.power_off, self.instance)

    def test_resume_state_on_host_boot(self):
        self._create_vm()
        self.mox.StubOutWithMock(vm_util, 'get_vm_state_from_name')
        self.mox.StubOutWithMock(self.conn, "reboot")
        vm_util.get_vm_state_from_name(mox.IgnoreArg(),
            self.instance['uuid']).AndReturn("poweredOff")
        self.conn.reboot(self.context, self.instance, 'network_info',
            'hard', None)
        self.mox.ReplayAll()
        self.conn.resume_state_on_host_boot(self.context, self.instance,
            'network_info')

    def test_resume_state_on_host_boot_no_reboot_1(self):
        """Don't call reboot on instance which is poweredon."""
        self._create_vm()
        self.mox.StubOutWithMock(vm_util, 'get_vm_state_from_name')
        self.mox.StubOutWithMock(self.conn, 'reboot')
        vm_util.get_vm_state_from_name(mox.IgnoreArg(),
            self.instance['uuid']).AndReturn("poweredOn")
        self.mox.ReplayAll()
        self.conn.resume_state_on_host_boot(self.context, self.instance,
            'network_info')

    def test_resume_state_on_host_boot_no_reboot_2(self):
        """Don't call reboot on instance which is suspended."""
        self._create_vm()
        self.mox.StubOutWithMock(vm_util, 'get_vm_state_from_name')
        self.mox.StubOutWithMock(self.conn, 'reboot')
        vm_util.get_vm_state_from_name(mox.IgnoreArg(),
            self.instance['uuid']).AndReturn("suspended")
        self.mox.ReplayAll()
        self.conn.resume_state_on_host_boot(self.context, self.instance,
            'network_info')

    def test_get_info(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_destroy(self):
        self._create_vm()
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        instances = self.conn.list_instances()
        self.assertEquals(len(instances), 1)
        self.conn.destroy(self.instance, self.network_info)
        instances = self.conn.list_instances()
        self.assertEquals(len(instances), 0)

    def test_destroy_non_existent(self):
        self._create_instance_in_the_db()
        self.assertEquals(self.conn.destroy(self.instance, self.network_info),
                          None)

    def _rescue(self):
        def fake_attach_disk_to_vm(*args, **kwargs):
            pass

        self._create_vm()
        info = self.conn.get_info({'name': 1, 'uuid': 'fake-uuid'})
        self.stubs.Set(self.conn._volumeops, "attach_disk_to_vm",
                       fake_attach_disk_to_vm)
        self.conn.rescue(self.context, self.instance, self.network_info,
                         self.image, 'fake-password')
        info = self.conn.get_info({'name-rescue': 1,
                                   'uuid': 'fake-uuid-rescue'})
        self._check_vm_info(info, power_state.RUNNING)
        info = self.conn.get_info({'name': 1, 'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.SHUTDOWN)

    def test_rescue(self):
        self._rescue()

    def test_unrescue(self):
        self._rescue()
        self.conn.unrescue(self.instance, None)
        info = self.conn.get_info({'name': 1, 'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_pause(self):
        pass

    def test_unpause(self):
        pass

    def test_diagnostics(self):
        pass

    def test_get_console_output(self):
        self._create_instance_in_the_db()
        res = self.conn.get_console_output(self.instance)
        self.assertNotEqual(0, len(res))

    def _test_finish_migration(self, power_on):
        """
        Tests the finish_migration method on vmops
        """

        self.power_on_called = False

        def fake_power_on(instance):
            self.assertEquals(self.instance, instance)
            self.power_on_called = True

        def fake_vmops_update_instance_progress(context, instance, step,
                                                total_steps):
            self.assertEquals(self.context, context)
            self.assertEquals(self.instance, instance)
            self.assertEquals(4, step)
            self.assertEqual(vmops.RESIZE_TOTAL_STEPS, total_steps)

        self.stubs.Set(self.conn._vmops, "_power_on", fake_power_on)
        self.stubs.Set(self.conn._vmops, "_update_instance_progress",
                       fake_vmops_update_instance_progress)

        # setup the test instance in the database
        self._create_vm()
        # perform the migration on our stubbed methods
        self.conn.finish_migration(context=self.context,
                                   migration=None,
                                   instance=self.instance,
                                   disk_info=None,
                                   network_info=None,
                                   block_device_info=None,
                                   resize_instance=False,
                                   image_meta=None,
                                   power_on=power_on)

    def test_finish_migration_power_on(self):
        self.assertRaises(NotImplementedError,
                          self._test_finish_migration, power_on=True)

    def test_finish_migration_power_off(self):
        self.assertRaises(NotImplementedError,
                          self._test_finish_migration, power_on=False)

    def _test_finish_revert_migration(self, power_on):
        """
        Tests the finish_revert_migration method on vmops
        """

        # setup the test instance in the database
        self._create_vm()

        self.power_on_called = False
        self.vm_name = str(self.instance['name']) + '-orig'

        def fake_power_on(instance):
            self.assertEquals(self.instance, instance)
            self.power_on_called = True

        def fake_get_orig_vm_name_label(instance):
            self.assertEquals(self.instance, instance)
            return self.vm_name

        def fake_get_vm_ref_from_name(session, vm_name):
            self.assertEquals(self.vm_name, vm_name)
            return vmwareapi_fake._get_objects("VirtualMachine").objects[0]

        def fake_get_vm_ref_from_uuid(session, vm_uuid):
            return vmwareapi_fake._get_objects("VirtualMachine").objects[0]

        def fake_call_method(*args, **kwargs):
            pass

        def fake_wait_for_task(*args, **kwargs):
            pass

        self.stubs.Set(self.conn._vmops, "_power_on", fake_power_on)
        self.stubs.Set(self.conn._vmops, "_get_orig_vm_name_label",
                       fake_get_orig_vm_name_label)
        self.stubs.Set(vm_util, "get_vm_ref_from_uuid",
                       fake_get_vm_ref_from_uuid)
        self.stubs.Set(vm_util, "get_vm_ref_from_name",
                       fake_get_vm_ref_from_name)
        self.stubs.Set(self.conn._session, "_call_method", fake_call_method)
        self.stubs.Set(self.conn._session, "_wait_for_task",
                       fake_wait_for_task)

        # perform the revert on our stubbed methods
        self.conn.finish_revert_migration(instance=self.instance,
                                          network_info=None,
                                          power_on=power_on)

    def test_finish_revert_migration_power_on(self):
        self.assertRaises(NotImplementedError,
                          self._test_finish_migration, power_on=True)

    def test_finish_revert_migration_power_off(self):
        self.assertRaises(NotImplementedError,
                          self._test_finish_migration, power_on=False)

    def test_diagnostics_non_existent_vm(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound,
                          self.conn.get_diagnostics,
                          self.instance)

    def test_get_console_pool_info(self):
        info = self.conn.get_console_pool_info("console_type")
        self.assertEquals(info['address'], 'test_url')
        self.assertEquals(info['username'], 'test_username')
        self.assertEquals(info['password'], 'test_pass')

    def test_get_vnc_console_non_existent(self):
        self._create_instance_in_the_db()
        self.assertRaises(exception.InstanceNotFound,
                          self.conn.get_vnc_console,
                          self.instance)

    def test_get_vnc_console(self):
        self._create_vm()
        fake_vm = vmwareapi_fake._get_objects("VirtualMachine").objects[0]
        fake_vm_id = int(fake_vm.obj.value.replace('vm-', ''))
        vnc_dict = self.conn.get_vnc_console(self.instance)
        self.assertEquals(vnc_dict['host'], 'test_url')
        self.assertEquals(vnc_dict['port'], cfg.CONF.vmware.vnc_port +
                          fake_vm_id % cfg.CONF.vmware.vnc_port_total)

    def test_host_ip_addr(self):
        self.assertEquals(self.conn.get_host_ip_addr(), "test_url")

    def test_get_volume_connector(self):
        self._create_vm()
        connector_dict = self.conn.get_volume_connector(self.instance)
        fake_vm = vmwareapi_fake._get_objects("VirtualMachine").objects[0]
        fake_vm_id = fake_vm.obj.value
        self.assertEquals(connector_dict['ip'], 'test_url')
        self.assertEquals(connector_dict['initiator'], 'iscsi-name')
        self.assertEquals(connector_dict['host'], 'test_url')
        self.assertEquals(connector_dict['instance'], fake_vm_id)

    def _test_vmdk_connection_info(self, type):
        return {'driver_volume_type': type,
                'serial': 'volume-fake-id',
                'data': {'volume': 'vm-10',
                         'volume_id': 'volume-fake-id'}}

    def test_volume_attach_vmdk(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('vmdk')
        mount_point = '/dev/vdc'
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_attach_volume_vmdk')
        volumeops.VMwareVolumeOps._attach_volume_vmdk(connection_info,
                self.instance, mount_point)
        self.mox.ReplayAll()
        self.conn.attach_volume(None, connection_info, self.instance,
                                mount_point)

    def test_volume_detach_vmdk(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('vmdk')
        mount_point = '/dev/vdc'
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_detach_volume_vmdk')
        volumeops.VMwareVolumeOps._detach_volume_vmdk(connection_info,
                self.instance, mount_point)
        self.mox.ReplayAll()
        self.conn.detach_volume(connection_info, self.instance, mount_point)

    def test_attach_vmdk_disk_to_vm(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('vmdk')
        mount_point = '/dev/vdc'
        discover = ('fake_name', 'fake_uuid')

        # create fake backing info
        volume_device = vmwareapi_fake.DataObject()
        volume_device.backing = vmwareapi_fake.DataObject()
        volume_device.backing.fileName = 'fake_path'

        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_get_vmdk_base_volume_device')
        volumeops.VMwareVolumeOps._get_vmdk_base_volume_device(
                mox.IgnoreArg()).AndReturn(volume_device)
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'attach_disk_to_vm')
        volumeops.VMwareVolumeOps.attach_disk_to_vm(mox.IgnoreArg(),
                self.instance, mox.IgnoreArg(), mox.IgnoreArg(),
                vmdk_path='fake_path',
                controller_key=mox.IgnoreArg(),
                unit_number=mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conn.attach_volume(None, connection_info, self.instance,
                                mount_point)

    def test_detach_vmdk_disk_from_vm(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('vmdk')
        mount_point = '/dev/vdc'
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_get_volume_uuid')
        volumeops.VMwareVolumeOps._get_volume_uuid(mox.IgnoreArg(),
                'volume-fake-id').AndReturn('fake_disk_uuid')
        self.mox.StubOutWithMock(vm_util, 'get_vmdk_backed_disk_device')
        vm_util.get_vmdk_backed_disk_device(mox.IgnoreArg(),
                'fake_disk_uuid').AndReturn('fake_device')
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_consolidate_vmdk_volume')
        volumeops.VMwareVolumeOps._consolidate_vmdk_volume(self.instance,
                 mox.IgnoreArg(), 'fake_device', mox.IgnoreArg())
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'detach_disk_from_vm')
        volumeops.VMwareVolumeOps.detach_disk_from_vm(mox.IgnoreArg(),
                self.instance, mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conn.detach_volume(connection_info, self.instance, mount_point)

    def test_volume_attach_iscsi(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('iscsi')
        mount_point = '/dev/vdc'
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_attach_volume_iscsi')
        volumeops.VMwareVolumeOps._attach_volume_iscsi(connection_info,
                self.instance, mount_point)
        self.mox.ReplayAll()
        self.conn.attach_volume(None, connection_info, self.instance,
                                mount_point)

    def test_volume_detach_iscsi(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('iscsi')
        mount_point = '/dev/vdc'
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 '_detach_volume_iscsi')
        volumeops.VMwareVolumeOps._detach_volume_iscsi(connection_info,
                self.instance, mount_point)
        self.mox.ReplayAll()
        self.conn.detach_volume(connection_info, self.instance, mount_point)

    def test_attach_iscsi_disk_to_vm(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('iscsi')
        connection_info['data']['target_portal'] = 'fake_target_portal'
        connection_info['data']['target_iqn'] = 'fake_target_iqn'
        mount_point = '/dev/vdc'
        discover = ('fake_name', 'fake_uuid')
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'discover_st')
        volumeops.VMwareVolumeOps.discover_st(
                connection_info['data']).AndReturn(discover)
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'attach_disk_to_vm')
        volumeops.VMwareVolumeOps.attach_disk_to_vm(mox.IgnoreArg(),
                self.instance, mox.IgnoreArg(), 'rdmp',
                controller_key=mox.IgnoreArg(),
                unit_number=mox.IgnoreArg(),
                device_name=mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conn.attach_volume(None, connection_info, self.instance,
                                mount_point)

    def test_detach_iscsi_disk_from_vm(self):
        self._create_vm()
        connection_info = self._test_vmdk_connection_info('iscsi')
        connection_info['data']['target_portal'] = 'fake_target_portal'
        connection_info['data']['target_iqn'] = 'fake_target_iqn'
        mount_point = '/dev/vdc'
        find = ('fake_name', 'fake_uuid')
        self.mox.StubOutWithMock(volume_util, 'find_st')
        volume_util.find_st(mox.IgnoreArg(), connection_info['data'],
                mox.IgnoreArg()).AndReturn(find)
        self.mox.StubOutWithMock(vm_util, 'get_rdm_disk')
        device = 'fake_device'
        vm_util.get_rdm_disk(mox.IgnoreArg(), 'fake_uuid').AndReturn(device)
        self.mox.StubOutWithMock(volumeops.VMwareVolumeOps,
                                 'detach_disk_from_vm')
        volumeops.VMwareVolumeOps.detach_disk_from_vm(mox.IgnoreArg(),
                self.instance, device)
        self.mox.ReplayAll()
        self.conn.detach_volume(connection_info, self.instance, mount_point)


class VMwareAPIHostTestCase(test.TestCase):
    """Unit tests for Vmware API host calls."""

    def setUp(self):
        super(VMwareAPIHostTestCase, self).setUp()
        self.flags(host_ip='test_url',
                   host_username='test_username',
                   host_password='test_pass', group='vmware')
        vmwareapi_fake.reset()
        stubs.set_stubs(self.stubs)
        self.conn = driver.VMwareESXDriver(False)

    def tearDown(self):
        super(VMwareAPIHostTestCase, self).tearDown()
        vmwareapi_fake.cleanup()

    def test_host_state(self):
        stats = self.conn.get_host_stats()
        self.assertEquals(stats['vcpus'], 16)
        self.assertEquals(stats['disk_total'], 1024)
        self.assertEquals(stats['disk_available'], 500)
        self.assertEquals(stats['disk_used'], 1024 - 500)
        self.assertEquals(stats['host_memory_total'], 1024)
        self.assertEquals(stats['host_memory_free'], 1024 - 500)
        supported_instances = [('i686', 'vmware', 'hvm'),
                               ('x86_64', 'vmware', 'hvm')]
        self.assertEquals(stats['supported_instances'], supported_instances)

    def _test_host_action(self, method, action, expected=None):
        result = method('host', action)
        self.assertEqual(result, expected)

    def test_host_reboot(self):
        self._test_host_action(self.conn.host_power_action, 'reboot')

    def test_host_shutdown(self):
        self._test_host_action(self.conn.host_power_action, 'shutdown')

    def test_host_startup(self):
        self._test_host_action(self.conn.host_power_action, 'startup')

    def test_host_maintenance_on(self):
        self._test_host_action(self.conn.host_maintenance_mode, True)

    def test_host_maintenance_off(self):
        self._test_host_action(self.conn.host_maintenance_mode, False)

    def test_get_host_uptime(self):
        result = self.conn.get_host_uptime('host')
        self.assertEqual('Please refer to test_url for the uptime', result)


class VMwareAPIVCDriverTestCase(VMwareAPIVMTestCase):

    def setUp(self):
        super(VMwareAPIVCDriverTestCase, self).setUp()
        cluster_name = 'test_cluster'
        cluster_name2 = 'test_cluster2'
        self.flags(cluster_name=[cluster_name, cluster_name2],
                   task_poll_interval=10, datastore_regex='.*', group='vmware')
        self.flags(vnc_enabled=False)
        self.conn = driver.VMwareVCDriver(None, False)
        node = self.conn._resources.keys()[0]
        self.node_name = '%s(%s)' % (node,
                                     self.conn._resources[node]['name'])
        node = self.conn._resources.keys()[1]
        self.node_name2 = '%s(%s)' % (node,
                                      self.conn._resources[node]['name'])

    def tearDown(self):
        super(VMwareAPIVCDriverTestCase, self).tearDown()
        vmwareapi_fake.cleanup()

    def test_get_available_resource(self):
        stats = self.conn.get_available_resource(self.node_name)
        self.assertEquals(stats['vcpus'], 16)
        self.assertEquals(stats['local_gb'], 1024)
        self.assertEquals(stats['local_gb_used'], 1024 - 500)
        self.assertEquals(stats['memory_mb'], 1024)
        self.assertEquals(stats['memory_mb_used'], 1024 - 524)
        self.assertEquals(stats['hypervisor_type'], 'VMware ESXi')
        self.assertEquals(stats['hypervisor_version'], '5.0.0')
        self.assertEquals(stats['hypervisor_hostname'], self.node_name)
        self.assertEquals(stats['supported_instances'],
                '[["i686", "vmware", "hvm"], ["x86_64", "vmware", "hvm"]]')

    def test_invalid_datastore_regex(self):
        # Tests if we raise an exception for Invalid Regular Expression in
        # vmware_datastore_regex
        self.flags(cluster_name=['test_cluster'], datastore_regex='fake-ds(01',
                   group='vmware')
        self.assertRaises(exception.InvalidInput, driver.VMwareVCDriver, None)

    def test_get_available_nodes(self):
        nodelist = self.conn.get_available_nodes()
        self.assertEquals(nodelist, [self.node_name, self.node_name2])

    def test_spawn_multiple_node(self):
        self._create_vm(node=self.node_name, num_instances=1)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)
        self._create_vm(node=self.node_name2, num_instances=2)
        info = self.conn.get_info({'uuid': 'fake-uuid'})
        self._check_vm_info(info, power_state.RUNNING)

    def test_finish_migration_power_on(self):
        self._test_finish_migration(power_on=True)
        self.assertEquals(True, self.power_on_called)

    def test_finish_migration_power_off(self):
        self._test_finish_migration(power_on=False)
        self.assertEquals(False, self.power_on_called)

    def test_finish_revert_migration_power_on(self):
        self._test_finish_revert_migration(power_on=True)
        self.assertEquals(True, self.power_on_called)

    def test_finish_revert_migration_power_off(self):
        self._test_finish_revert_migration(power_on=False)
        self.assertEquals(False, self.power_on_called)

    def test_get_vnc_console(self):
        self._create_instance_in_the_db()
        self._create_vm()
        fake_vm = vmwareapi_fake._get_objects("VirtualMachine").objects[0]
        fake_vm_id = int(fake_vm.obj.value.replace('vm-', ''))
        vnc_dict = self.conn.get_vnc_console(self.instance)
        self.assertEquals(vnc_dict['host'], "ha-host")
        self.assertEquals(vnc_dict['port'], cfg.CONF.vmware.vnc_port +
                          fake_vm_id % cfg.CONF.vmware.vnc_port_total)
