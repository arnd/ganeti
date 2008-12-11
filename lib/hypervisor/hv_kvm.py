#
#

# Copyright (C) 2008 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""KVM hypervisor

"""

import os
import os.path
import re
import tempfile
from cStringIO import StringIO

from ganeti import utils
from ganeti import constants
from ganeti import errors
from ganeti.hypervisor import hv_base


class KVMHypervisor(hv_base.BaseHypervisor):
  """Fake hypervisor interface.

  This can be used for testing the ganeti code without having to have
  a real virtualisation software installed.

  """
  _ROOT_DIR = constants.RUN_GANETI_DIR + "/kvm-hypervisor"
  _PIDS_DIR = _ROOT_DIR + "/pid"
  _CTRL_DIR = _ROOT_DIR + "/ctrl"
  _DIRS = [_ROOT_DIR, _PIDS_DIR, _CTRL_DIR]

  PARAMETERS = [
    constants.HV_KERNEL_PATH,
    constants.HV_INITRD_PATH,
    constants.HV_ACPI,
    ]

  def __init__(self):
    hv_base.BaseHypervisor.__init__(self)
    # Let's make sure the directories we need exist, even if the RUN_DIR lives
    # in a tmpfs filesystem or has been otherwise wiped out.
    for dir in self._DIRS:
      if not os.path.exists(dir):
        os.mkdir(dir)

  def _WriteNetScript(self, instance, seq, nic):
    """Write a script to connect a net interface to the proper bridge.

    This can be used by any qemu-type hypervisor.

    @param instance: instance we're acting on
    @type instance: instance object
    @param seq: nic sequence number
    @type seq: int
    @param nic: nic we're acting on
    @type nic: nic object
    @return: netscript file name
    @rtype: string

    """
    script = StringIO()
    script.write("#!/bin/sh\n")
    script.write("# this is autogenerated by Ganeti, please do not edit\n#\n")
    script.write("export INSTANCE=%s\n" % instance.name)
    script.write("export MAC=%s\n" % nic.mac)
    script.write("export IP=%s\n" % nic.ip)
    script.write("export BRIDGE=%s\n" % nic.bridge)
    script.write("export INTERFACE=$1\n")
    # TODO: make this configurable at ./configure time
    script.write("if [ -x /etc/ganeti/kvm-vif-bridge ]; then\n")
    script.write("  # Execute the user-specific vif file\n")
    script.write("  /etc/ganeti/kvm-vif-bridge\n")
    script.write("else\n")
    script.write("  # Connect the interface to the bridge\n")
    script.write("  /sbin/ifconfig $INTERFACE 0.0.0.0 up\n")
    script.write("  /usr/sbin/brctl addif $BRIDGE $INTERFACE\n")
    script.write("fi\n\n")
    # As much as we'd like to put this in our _ROOT_DIR, that will happen to be
    # mounted noexec sometimes, so we'll have to find another place.
    (tmpfd, tmpfile_name) = tempfile.mkstemp()
    tmpfile = os.fdopen(tmpfd, 'w')
    tmpfile.write(script.getvalue())
    tmpfile.close()
    os.chmod(tmpfile_name, 0755)
    return tmpfile_name

  def ListInstances(self):
    """Get the list of running instances.

    We can do this by listing our live instances directory and
    checking whether the associated kvm process is still alive.

    """
    result = []
    for name in os.listdir(self._PIDS_DIR):
      file = "%s/%s" % (self._PIDS_DIR, name)
      if utils.IsProcessAlive(utils.ReadPidFile(file)):
        result.append(name)
    return result

  def GetInstanceInfo(self, instance_name):
    """Get instance properties.

    @param instance_name: the instance name

    @return: tuple (name, id, memory, vcpus, stat, times)

    """
    pidfile = "%s/%s" % (self._PIDS_DIR, instance_name)
    pid = utils.ReadPidFile(pidfile)
    if not utils.IsProcessAlive(pid):
      return None

    cmdline_file = "/proc/%s/cmdline" % pid
    try:
      fh = open(cmdline_file, 'r')
      try:
        cmdline = fh.read()
      finally:
        fh.close()
    except IOError, err:
      raise errors.HypervisorError("Failed to list instance %s: %s" %
                                   (instance_name, err))

    memory = 0
    vcpus = 0
    stat = "---b-"
    times = "0"

    arg_list = cmdline.split('\x00')
    while arg_list:
      arg =  arg_list.pop(0)
      if arg == '-m':
        memory = arg_list.pop(0)
      elif arg == '-smp':
        vcpus = arg_list.pop(0)

    return (instance_name, pid, memory, vcpus, stat, times)

  def GetAllInstancesInfo(self):
    """Get properties of all instances.

    @return: list of tuples (name, id, memory, vcpus, stat, times)

    """
    data = []
    for name in os.listdir(self._PIDS_DIR):
      file = "%s/%s" % (self._PIDS_DIR, name)
      if utils.IsProcessAlive(utils.ReadPidFile(file)):
        data.append(self.GetInstanceInfo(name))

    return data

  def StartInstance(self, instance, block_devices, extra_args):
    """Start an instance.

    """
    temp_files = []
    pidfile = self._PIDS_DIR + "/%s" % instance.name
    if utils.IsProcessAlive(utils.ReadPidFile(pidfile)):
      raise errors.HypervisorError("Failed to start instance %s: %s" %
                                   (instance.name, "already running"))

    kvm = constants.KVM_PATH
    kvm_cmd = [kvm]
    kvm_cmd.extend(['-m', instance.beparams[constants.BE_MEMORY]])
    kvm_cmd.extend(['-smp', instance.beparams[constants.BE_VCPUS]])
    kvm_cmd.extend(['-pidfile', pidfile])
    # used just by the vnc server, if enabled
    kvm_cmd.extend(['-name', instance.name])
    kvm_cmd.extend(['-daemonize'])
    if not instance.hvparams[constants.HV_ACPI]:
      kvm_cmd.extend(['-no-acpi'])
    if not instance.nics:
      kvm_cmd.extend(['-net', 'none'])
    else:
      nic_seq = 0
      for nic in instance.nics:
        script = self._WriteNetScript(instance, nic_seq, nic)
        # FIXME: handle other models
        nic_val = "nic,macaddr=%s,model=virtio" % nic.mac
        kvm_cmd.extend(['-net', nic_val])
        kvm_cmd.extend(['-net', 'tap,script=%s' % script])
        temp_files.append(script)
        nic_seq += 1

    boot_drive = True
    for cfdev, rldev in block_devices:
      # TODO: handle FD_LOOP and FD_BLKTAP (?)
      if boot_drive:
        boot_val = ',boot=on'
        boot_drive = False
      else:
        boot_val = ''

      # TODO: handle different if= types
      if_val = ',if=virtio'

      drive_val = 'file=%s,format=raw%s%s' % (rldev.dev_path, if_val, boot_val)
      kvm_cmd.extend(['-drive', drive_val])

    kvm_cmd.extend(['-kernel', instance.hvparams[constants.HV_KERNEL_PATH]])

    initrd_path = instance.hvparams[constants.HV_INITRD_PATH]
    if initrd_path:
      kvm_cmd.extend(['-initrd', initrd_path])

    kvm_cmd.extend(['-append', 'console=ttyS0,38400 root=/dev/vda'])

    #"hvm_boot_order",
    #"hvm_cdrom_image_path",

    kvm_cmd.extend(['-nographic'])
    # FIXME: handle vnc, if needed
    # How do we decide whether to have it or not?? :(
    #"vnc_bind_address",
    #"network_port"
    base_control = '%s/%s' % (self._CTRL_DIR, instance.name)
    monitor_dev = 'unix:%s.monitor,server,nowait' % base_control
    kvm_cmd.extend(['-monitor', monitor_dev])
    serial_dev = 'unix:%s.serial,server,nowait' % base_control
    kvm_cmd.extend(['-serial', serial_dev])

    result = utils.RunCmd(kvm_cmd)
    if result.failed:
      raise errors.HypervisorError("Failed to start instance %s: %s (%s)" %
                                   (instance.name, result.fail_reason,
                                    result.output))

    if not utils.IsProcessAlive(utils.ReadPidFile(pidfile)):
      raise errors.HypervisorError("Failed to start instance %s: %s" %
                                   (instance.name))

    for file in temp_files:
      utils.RemoveFile(file)

  def StopInstance(self, instance, force=False):
    """Stop an instance.

    """
    pid_file = self._PIDS_DIR + "/%s" % instance.name
    pid = utils.ReadPidFile(pid_file)
    if pid > 0 and utils.IsProcessAlive(pid):
      if force or not instance.hvparams[constants.HV_ACPI]:
        utils.KillProcess(pid)
      else:
        # This only works if the instance os has acpi support
        monitor_socket = '%s/%s.monitor'  % (self._CTRL_DIR, instance.name)
        socat = 'socat -u STDIN UNIX-CONNECT:%s' % monitor_socket
        command = "echo 'system_powerdown' | %s" % socat
        result = utils.RunCmd(command)
        if result.failed:
          raise errors.HypervisorError("Failed to stop instance %s: %s" %
                                       (instance.name, result.fail_reason))

    if not utils.IsProcessAlive(pid):
      utils.RemoveFile(pid_file)

  def RebootInstance(self, instance):
    """Reboot an instance.

    """
    # For some reason if we do a 'send-key ctrl-alt-delete' to the control
    # socket the instance will stop, but now power up again. So we'll resort
    # to shutdown and restart.
    self.StopInstance(instance)
    self.StartInstance(instance)

  def GetNodeInfo(self):
    """Return information about the node.

    @return: a dict with the following keys (values in MiB):
          - memory_total: the total memory size on the node
          - memory_free: the available memory on the node for instances
          - memory_dom0: the memory used by the node itself, if available

    """
    # global ram usage from the xm info command
    # memory                 : 3583
    # free_memory            : 747
    # note: in xen 3, memory has changed to total_memory
    try:
      fh = file("/proc/meminfo")
      try:
        data = fh.readlines()
      finally:
        fh.close()
    except IOError, err:
      raise errors.HypervisorError("Failed to list node info: %s" % err)

    result = {}
    sum_free = 0
    for line in data:
      splitfields = line.split(":", 1)

      if len(splitfields) > 1:
        key = splitfields[0].strip()
        val = splitfields[1].strip()
        if key == 'MemTotal':
          result['memory_total'] = int(val.split()[0])/1024
        elif key in ('MemFree', 'Buffers', 'Cached'):
          sum_free += int(val.split()[0])/1024
        elif key == 'Active':
          result['memory_dom0'] = int(val.split()[0])/1024
    result['memory_free'] = sum_free

    cpu_total = 0
    try:
      fh = open("/proc/cpuinfo")
      try:
        cpu_total = len(re.findall("(?m)^processor\s*:\s*[0-9]+\s*$",
                                   fh.read()))
      finally:
        fh.close()
    except EnvironmentError, err:
      raise errors.HypervisorError("Failed to list node info: %s" % err)
    result['cpu_total'] = cpu_total

    return result

  @staticmethod
  def GetShellCommandForConsole(instance):
    """Return a command for connecting to the console of an instance.

    """
    # TODO: we can either try the serial socket or suggest vnc
    return "echo Console not available for the kvm hypervisor yet"

  def Verify(self):
    """Verify the hypervisor.

    Check that the binary exists.

    """
    if not os.path.exists(constants.KVM_PATH):
      return "The kvm binary ('%s') does not exist." % constants.KVM_PATH

  @classmethod
  def CheckParameterSyntax(cls, hvparams):
    """Check the given parameters for validity.

    For the KVM hypervisor, this only check the existence of the
    kernel.

    @type hvparams:  dict
    @param hvparams: dictionary with parameter names/value
    @raise errors.HypervisorError: when a parameter is not valid

    """
    super(KVMHypervisor, cls).CheckParameterSyntax(hvparams)

    if not hvparams[constants.HV_KERNEL_PATH]:
      raise errors.HypervisorError("Need a kernel for the instance")

    if not os.path.isabs(hvparams[constants.HV_KERNEL_PATH]):
      raise errors.HypervisorError("The kernel path must an absolute path")

    if hvparams[constants.HV_INITRD_PATH]:
      if not os.path.isabs(hvparams[constants.HV_INITRD_PATH]):
        raise errors.HypervisorError("The initrd path must an absolute path"
                                     ", if defined")

  def ValidateParameters(self, hvparams):
    """Check the given parameters for validity.

    For the KVM hypervisor, this checks the existence of the
    kernel.

    """
    super(KVMHypervisor, self).ValidateParameters(hvparams)

    kernel_path = hvparams[constants.HV_KERNEL_PATH]
    if not os.path.isfile(kernel_path):
      raise errors.HypervisorError("Instance kernel '%s' not found or"
                                   " not a file" % kernel_path)
    initrd_path = hvparams[constants.HV_INITRD_PATH]
    if initrd_path and not os.path.isfile(initrd_path):
      raise errors.HypervisorError("Instance initrd '%s' not found or"
                                   " not a file" % initrd_path)
