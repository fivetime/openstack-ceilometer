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
"""A small client for the parts of Zun's API that metering reads.

openstacksdk knows Zun's service type but has no proxy for it, so this
speaks to the API through a keystoneauth adapter. It asks for API 1.56,
the first version with a whole host's readings in one call.
"""

from keystoneauth1 import adapter
from oslo_config import cfg

from ceilometer import keystone_client

SERVICE_OPTS = [
    cfg.StrOpt('zun',
               default='container',
               help='Zun service type.'),
]

#: The first Zun API version with GET /v1/hosts/{host}/stats.
API_VERSION = '1.56'


class Client:
    """Reads Zun's hosts and what runs on them."""

    def __init__(self, conf):
        creds = conf.service_credentials
        self.api = adapter.Adapter(
            session=keystone_client.get_session(conf),
            service_type=conf.service_types.zun,
            interface=creds.interface,
            region_name=creds.region_name,
            default_microversion=API_VERSION)

    def hosts(self):
        """The name of every Zun compute host, following the pages."""
        names, marker = [], None
        while True:
            params = {'marker': marker} if marker else {}
            page = self.api.get('/v1/hosts', params=params).json()
            hosts = page.get('hosts') or []
            names.extend(h['hostname'] for h in hosts if h.get('hostname'))
            if not page.get('next') or not hosts:
                return names
            marker = hosts[-1]['uuid']

    def host_stats(self, host, kinds, label=None):
        """One host's objects of these kinds, read now.

        Answers {'host', 'results': [...], 'unavailable': [...]}; see
        Zun's GET /v1/hosts/{host}/stats.
        """
        params = {'type': ','.join(kinds)}
        if label:
            params['label'] = label
        return self.api.get('/v1/hosts/%s/stats' % host,
                            params=params).json()
