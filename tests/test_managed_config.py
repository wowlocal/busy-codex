import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import report


class ManagedConfigTest(unittest.TestCase):
    def test_managed_runner_cannot_inherit_a_previous_installations_hub(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'env.sh').write_text(
                'BUSYBAR_HUB=http://old-host:8765\nBUSYBAR_PORT=8765\n')
            with mock.patch.object(report, 'HERE', Path(directory)), \
                 mock.patch.dict(os.environ, {'BUSYBAR_MANAGED': '1',
                                              'BUSYBAR_PORT': '18765'}, clear=True):
                env = report.load_env()
            self.assertNotIn('BUSYBAR_HUB', env)
            self.assertEqual('18765', env['BUSYBAR_PORT'])

    def test_existing_installations_still_read_their_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'env.sh').write_text('BUSYBAR_PORT=8766\n')
            with mock.patch.object(report, 'HERE', Path(directory)), \
                 mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual('8766', report.load_env()['BUSYBAR_PORT'])


if __name__ == '__main__':
    unittest.main()
