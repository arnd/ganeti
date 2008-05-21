#!/usr/bin/python
#

# Copyright (C) 2006, 2007, 2008 Google Inc.
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


"""Remote API resources.

"""

import simplejson
import cgi
import re

import ganeti.opcodes
import ganeti.errors
import ganeti.utils
import ganeti.cli

from ganeti import constants


# Initialized at the end of this file.
_CONNECTOR = {}


class RemoteAPIError(ganeti.errors.GenericError):
  """Remote API exception.

  """


def BuildUriList(names, uri_format):
  """Builds a URI list as used by index resources.

  Args:
  - names: List of names as strings
  - uri_format: Format to be applied for URI

  """
  def _MapName(name):
    return { "name": name, "uri": uri_format % name, }

  # Make sure the result is sorted, makes it nicer to look at and simplifies
  # unittests.
  names.sort()

  return map(_MapName, names)


def ExtractField(sequence, index):
  """Creates a list containing one column out of a list of lists

  Args:
  - sequence: Sequence of lists
  - index: Index of field

  """
  return map(lambda item: item[index], sequence)


def MapFields(names, data):
  """Maps two lists into one dictionary.

  Args:
  - names: Field names (list of strings)
  - data: Field data (list)

  Example:
  >>> MapFields(["a", "b"], ["foo", 123])
  {'a': 'foo', 'b': 123}

  """
  if len(names) != len(data):
    raise AttributeError("Names and data must have the same length")
  return dict([(names[i], data[i]) for i in range(len(names))])


class Mapper:
  """Map resource to method.

  """
  def __init__(self, connector=_CONNECTOR):
    """Resource mapper constructor.

    Args:
      con: a dictionary, mapping method name with URL path regexp

    """
    self._connector = connector

  def getController(self, uri):
    """Find method for a given URI.

    Args:
      uri: string with URI

    Returns:
      None if no method is found or a tuple containing the following fields:
        methd: name of method mapped to URI
        items: a list of variable intems in the path
        args: a dictionary with additional parameters from URL

    """
    if '?' in uri:
      (path, query) = uri.split('?', 1)
      args = cgi.parse_qs(query)
    else:
      path = uri
      query = None
      args = {}

    result = None

    for key, handler in self._connector.iteritems():
      # Regex objects
      if hasattr(key, "match"):
        m = key.match(path)
        if m:
          result = (handler, list(m.groups()), args)
          break

      # String objects
      elif key == path:
        result = (handler, [], args)
        break

    return result


class R_Generic(object):
  """Generic class for resources.

  """
  LOCK = 'cmd'

  def __init__(self, dispatcher, items, args):
    """Generic resource constructor.

    Args:
      dispatcher: HTTPRequestHandler object
      items: a list with variables encoded in the URL
      args: a dictionary with additional options from URL

    """
    self.dispatcher = dispatcher
    self.items = items
    self.args = args
    self.code = 200
    self.result = None

  def set_headers(self, headers):
    """Pass headers to the resource class.

    Args:
      headers: a Message-like class used to parse headers
    """
    self.headers = headers

  def do_Request(self, request):
    """Default request flow.

    """
    try:
      fn = getattr(self, '_%s' % request.lower())
      if self.LOCK:
        ganeti.utils.Lock(self.LOCK, max_retries=15)
        try:
          fn()
        finally:
          ganeti.utils.Unlock(self.LOCK)
          ganeti.utils.LockCleanup()
      else:
        fn()
      self.send(self.code, self.result)

    except RemoteAPIError, msg:
      self.send_error(self.code, str(msg))
    except ganeti.errors.OpPrereqError, msg:
      self.send_error(404, str(msg))
    except AttributeError, msg:
      self.send_error(405, 'Method Not Implemented: %s' % msg)
    except ganeti.errors.LockError, msg:
      self.send_error(503, 'Unable to acquire the lock: %s' % msg)
    except Exception, msg:
      self.send_error(500, 'Internal Server Error: %s' % msg)

  def send(self, code, data=None):
    """Write data to client.

    Args:
      code: int, the HTTP response code
      data: message body

    """
    self.dispatcher.send_response(code)
    # rfc4627.txt
    self.dispatcher.send_header("Content-type", "application/json")
    self.dispatcher.end_headers()
    if data:
      self.dispatcher.wfile.write(simplejson.dumps(data))

  def send_error(self, code, message):
    """Send an error to the client.

    Args:
      code: HTTP response code (int)
      message: Error message

    """
    self.dispatcher.send_error(code, message)


class R_root(R_Generic):
  """/ resource.

  """
  LOCK = None

  def _get(self):
    """Show the list of mapped resources.

    """
    root_pattern = re.compile('^R_([a-zA-Z0-9]+)$')

    rootlist = []
    for handler in _CONNECTOR.values():
      m = root_pattern.match(handler.__name__)
      if m:
        name = m.group(1)
        if name != 'root':
          rootlist.append(name)

    self.result = BuildUriList(rootlist, "/%s")


class R_version(R_Generic):
  """/version resource.

  """
  LOCK = None

  def _get(self):
    """Returns the remote API version.

    """
    self.result = constants.RAPI_VERSION


class R_instances(R_Generic):
  """/instances resource.

  """
  LOCK = None

  def _get(self):
    """Send a list of all available instances.

    """
    op = ganeti.opcodes.OpQueryInstances(output_fields=["name"], names=[])
    instancelist = ganeti.cli.SubmitOpCode(op)

    self.result = BuildUriList(ExtractField(instancelist, 0),
                               "/instances/%s")


class R_tags(R_Generic):
  """/tags resource.

  """
  LOCK = None

  def _get(self):
    """Send a list of all cluster tags."""
    op = ganeti.opcodes.OpGetTags(kind=constants.TAG_CLUSTER)
    tags = ganeti.cli.SubmitOpCode(op)
    self.result = list(tags)


class R_info(R_Generic):
  """Cluster info.

  """
  LOCK = None

  def _get(self):
    """Returns cluster information.

    """
    op = ganeti.opcodes.OpQueryClusterInfo()
    self.result = ganeti.cli.SubmitOpCode(op)


class R_nodes(R_Generic):
  """/nodes resource.

  """
  LOCK = None

  def _get(self):
    """Send a list of all nodes.

    """
    op = ganeti.opcodes.OpQueryNodes(output_fields=["name"], names=[])
    nodelist = ganeti.cli.SubmitOpCode(op)

    self.result = BuildUriList(ExtractField(nodelist, 0),
                               "/nodes/%s")


class R_nodes_name(R_Generic):
  """/nodes/[node_name] resources.

  """
  def _get(self):
    """Send information about a node.

    """
    node_name = self.items[0]
    fields = ["dtotal", "dfree",
              "mtotal", "mnode", "mfree",
              "pinst_cnt", "sinst_cnt"]

    op = ganeti.opcodes.OpQueryNodes(output_fields=fields,
                                     names=[node_name])
    result = ganeti.cli.SubmitOpCode(op)

    self.result = MapFields(fields, result[0])


class R_nodes_name_tags(R_Generic):
  """/nodes/[node_name]/tags resource.

  """
  LOCK = None

  def _get(self):
    """Send a list of node tags.

    """
    op = ganeti.opcodes.OpGetTags(kind=constants.TAG_NODE, name=self.items[0])
    tags = ganeti.cli.SubmitOpCode(op)
    self.result = list(tags)


class R_instances_name(R_Generic):
  """/instances/[instance_name] resources.

  """
  def _get(self):
    """Send information about an instance.

    """
    instance_name = self.items[0]
    fields = ["name", "os", "pnode", "snodes",
              "admin_state", "admin_ram",
              "disk_template", "ip", "mac", "bridge",
              "sda_size", "sdb_size", "vcpus",
              "oper_state", "status"]

    op = ganeti.opcodes.OpQueryInstances(output_fields=fields,
                                         names=[instance_name])
    result = ganeti.cli.SubmitOpCode(op)

    self.result = MapFields(fields, result[0])


class R_instances_name_tags(R_Generic):
  """/instances/[instance_name]/tags resource.

  """
  LOCK = None

  def _get(self):
    """Send a list of instance tags.

    """
    op = ganeti.opcodes.OpGetTags(kind=constants.TAG_INSTANCE,
                                  name=self.items[0])
    tags = ganeti.cli.SubmitOpCode(op)
    self.result = list(tags)


class R_os(R_Generic):
  """/os resource.

  """
  def _get(self):
    """Send a list of all OSes.

    """
    op = ganeti.opcodes.OpDiagnoseOS(output_fields=["name", "valid"],
                                     names=[])
    diagnose_data = ganeti.cli.SubmitOpCode(op)

    if not isinstance(diagnose_data, list):
      self.code = 500
      raise RemoteAPIError("Can't get the OS list")

    self.result = [row[0] for row in diagnose_data if row[1]]


_CONNECTOR.update({
  "/": R_root,

  "/version": R_version,

  "/tags": R_tags,
  "/info": R_info,

  "/nodes": R_nodes,
  re.compile(r'^/nodes/([\w\._-]+)$'): R_nodes_name,
  re.compile(r'^/nodes/([\w\._-]+)/tags$'): R_nodes_name_tags,

  "/instances": R_instances,
  re.compile(r'^/instances/([\w\._-]+)$'): R_instances_name,
  re.compile(r'^/instances/([\w\._-]+)/tags$'): R_instances_name_tags,

  "/os": R_os,
  })
