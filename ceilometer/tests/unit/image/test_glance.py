#
# Copyright 2012 New Dream Network, LLC (DreamHost)
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

from ceilometer.image import glance
from ceilometer.polling import manager
from ceilometer import service
import ceilometer.tests.base as base
from ceilometer.tests.unit import fakes


class TestImagePollsterPageSize(base.BaseTestCase):
    def setUp(self):
        super().setUp()
        conf = service.prepare_service([], [])
        self.manager = manager.AgentManager(0, conf)
        self.pollster = glance.ImageSizePollster(conf)

    def test_image_pollster(self):
        image_samples = list(
            self.pollster.get_samples(
                self.manager, {}, resources=fakes.IMAGE_LIST))
        self.assertEqual(len(fakes.IMAGE_LIST), len(image_samples))
        for ix in range(len(image_samples)):
            self.assertEqual('image.size', image_samples[ix].name)
            self.assertEqual(fakes.IMAGE_LIST[ix].size,
                             image_samples[ix].volume)
            self.assertEqual(fakes.IMAGE_LIST[ix].owner,
                             image_samples[ix].project_id)
            self.assertEqual(fakes.IMAGE_LIST[ix].id,
                             image_samples[ix].resource_id)
