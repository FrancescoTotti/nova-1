# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Openstack, LLC.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
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
Scheduler base class that all Schedulers should inherit from
"""

import datetime

from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import rpc
from nova.compute import power_state

FLAGS = flags.FLAGS
flags.DEFINE_integer('service_down_time', 60,
                     'maximum time since last checkin for up service')
flags.DECLARE('instances_path', 'nova.compute.manager')


class NoValidHost(exception.Error):
    """There is no valid host for the command."""
    pass


class WillNotSchedule(exception.Error):
    """The specified host is not up or doesn't exist."""
    pass


class Scheduler(object):
    """The base class that all Scheduler clases should inherit from."""

    @staticmethod
    def service_is_up(service):
        """Check whether a service is up based on last heartbeat."""
        last_heartbeat = service['updated_at'] or service['created_at']
        # Timestamps in DB are UTC.
        elapsed = datetime.datetime.utcnow() - last_heartbeat
        return elapsed < datetime.timedelta(seconds=FLAGS.service_down_time)

    def hosts_up(self, context, topic):
        """Return the list of hosts that have a running service for topic."""

        services = db.service_get_all_by_topic(context, topic)
        return [service.host
                for service in services
                if self.service_is_up(service)]

    def schedule(self, context, topic, *_args, **_kwargs):
        """Must override at least this method for scheduler to work."""
        raise NotImplementedError(_("Must implement a fallback schedule"))

    def schedule_live_migration(self, context, instance_id, dest):
        """Live migration scheduling method.

        :param context:
        :param instance_id:
        :param dest: destination host
        :return:
            The host where instance is running currently.
            Then scheduler send request that host.

        """

        # Whether instance exists and is running.
        instance_ref = db.instance_get(context, instance_id)

        # Checking instance.
        self._live_migration_src_check(context, instance_ref)

        # Checking destination host.
        self._live_migration_dest_check(context, instance_ref, dest)

        # Common checking.
        self._live_migration_common_check(context, instance_ref, dest)

        # Changing instance_state.
        db.instance_set_state(context,
                              instance_id,
                              power_state.PAUSED,
                              'migrating')

        # Changing volume state
        for v in instance_ref['volumes']:
            db.volume_update(context,
                             v['id'],
                             {'status': 'migrating'})

        # Return value is necessary to send request to src
        # Check _schedule() in detail.
        src = instance_ref['host']
        return src

    def _live_migration_src_check(self, context, instance_ref):
        """Live migration check routine (for src host).

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object

        """

        # Checking instance is running.
        if (power_state.RUNNING != instance_ref['state'] or \
           'running' != instance_ref['state_description']):
            msg = _('Instance(%s) is not running')
            ec2_id = instance_ref['hostname']
            raise exception.Invalid(msg % ec2_id)

        # Checing volume node is running when any volumes are mounted
        # to the instance.
        if len(instance_ref['volumes']) != 0:
            services = db.service_get_all_by_topic(context, 'volume')
            if len(services) < 1 or  not self.service_is_up(services[0]):
                msg = _('volume node is not alive(time synchronize problem?)')
                raise exception.Invalid(msg)

        # Checking src host exists and compute node
        src = instance_ref['host']
        services = db.service_get_all_compute_by_host(context, src)

        # Checking src host is alive.
        if not self.service_is_up(services[0]):
            msg = _('%s is not alive(time synchronize problem?)')
            raise exception.Invalid(msg % src)

    def _live_migration_dest_check(self, context, instance_ref, dest):
        """Live migration check routine (for destination host).

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object
        :param dest: destination host

        """

        # Checking dest exists and compute node.
        dservice_refs = db.service_get_all_compute_by_host(context, dest)
        dservice_ref = dservice_refs[0]

        # Checking dest host is alive.
        if not self.service_is_up(dservice_ref):
            msg = _('%s is not alive(time synchronize problem?)')
            raise exception.Invalid(msg % dest)

        # Checking whether The host where instance is running
        # and dest is not same.
        src = instance_ref['host']
        if dest == src:
            ec2_id = instance_ref['hostname']
            raise exception.Invalid(_("%(dest)s is where %(ec2_id)s is "
                                       "running now. choose other host.")
                                       % locals())

        # Checking dst host still has enough capacities.
        self.has_enough_resources(context, instance_ref, dest)

    def _live_migration_common_check(self, context, instance_ref, dest):
        """Live migration common check routine.

        Below checkings are followed by
        http://wiki.libvirt.org/page/TodoPreMigrationChecks

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object
        :param dest: destination host

        """

        # Checking shared storage connectivity
        self.mounted_on_same_shared_storage(context, instance_ref, dest)

        # Checking dest exists.
        dservice_refs = db.service_get_all_compute_by_host(context, dest)
        dservice_ref = dservice_refs[0]['compute_service'][0]

        # Checking original host( where instance was launched at) exists.
        try:
            oservice_refs = db.service_get_all_compute_by_host(context,
                                           instance_ref['launched_on'])
        except exception.NotFound:
            raise exception.Invalid(_("host %s where instance was launched "
                                      "does not exist.")
                                       % instance_ref['launched_on'])
        oservice_ref = oservice_refs[0]['compute_service'][0]

        # Checking hypervisor is same.
        o = oservice_ref['hypervisor_type']
        d = dservice_ref['hypervisor_type']
        if o != d:
            raise exception.Invalid(_("Different hypervisor type"
                                      "(%(o)s->%(d)s)')" % locals()))

        # Checkng hypervisor version.
        o = oservice_ref['hypervisor_version']
        d = dservice_ref['hypervisor_version']
        if o > d:
            raise exception.Invalid(_('Older hypervisor version(%(o)s->%(d)s)')
                                    % locals())

        # Checking cpuinfo.
        try:
            rpc.call(context,
                     db.queue_get_for(context, FLAGS.compute_topic, dest),
                     {"method": 'compare_cpu',
                      "args": {'cpu_info': oservice_ref['cpu_info']}})

        except rpc.RemoteError, e:
            ec2_id = instance_ref['hostname']
            src = instance_ref['host']
            logging.exception(_("host %(dest)s is not compatible with "
                                "original host %(src)s.") % locals())
            raise

    def has_enough_resources(self, context, instance_ref, dest):
        """Checks if destination host has enough resource for live migration.

        Currently, only memory checking has been done.
        If storage migration(block migration, meaning live-migration
        without any shared storage) will be available, local storage
        checking is also necessary.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object
        :param dest: destination host

        """

        # Getting instance information
        ec2_id = instance_ref['hostname']

        # Getting host information
        service_refs = db.service_get_all_compute_by_host(context, dest)
        compute_service_ref = service_refs[0]['compute_service'][0]

        mem_total = int(compute_service_ref['memory_mb'])
        mem_used = int(compute_service_ref['memory_mb_used'])
        mem_avail = mem_total - mem_used
        mem_inst = instance_ref['memory_mb']
        if mem_avail <= mem_inst:
            raise exception.NotEmpty(_("%(ec2_id)s is not capable to "
                                       "migrate %(dest)s (host:%(mem_avail)s "
                                       " <= instance:%(mem_inst)s)")
                                       % locals())

    def mounted_on_same_shared_storage(self, context, instance_ref, dest):
        """Check if the src and dest host mount same shared storage.

        At first, dest host creates temp file, and src host can see
        it if they mounts same shared storage. Then src host erase it.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object
        :param dest: destination host

        """

        src = instance_ref['host']
        dst_t = db.queue_get_for(context, FLAGS.compute_topic, dest)
        src_t = db.queue_get_for(context, FLAGS.compute_topic, src)

        # create tmpfile at dest host
        try:
            filename = rpc.call(context, dst_t, {"method": 'mktmpfile'})
        except rpc.RemoteError, e:
            msg = _("Cannot create tmpfile at %s to confirm shared storage.")
            LOG.error(msg % FLAGS.instances_path)
            raise

        # make sure existence at src host.
        try:
            rpc.call(context, src_t,
                     {"method": 'confirm_tmpfile', "args": {'path': filename}})

        except (rpc.RemoteError, exception.NotFound), e:
            ipath = FLAGS.instances_path
            logging.error(_("Cannot comfirm %(ipath)s at %(dest)s is "
                            "located at same shared storage.") % locals())
            raise
