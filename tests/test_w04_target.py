import unittest

from deploy.target import recorded_security_group_matches


class W04TargetTests(unittest.TestCase):
    def test_matching_recorded_security_group_passes(self):
        instance = {"SecurityGroups": [{"GroupId": "sg-expected"}]}
        self.assertTrue(recorded_security_group_matches(instance, {"security_group_id": "sg-expected"}))

    def test_mismatched_or_extra_security_group_fails_closed(self):
        instance = {"SecurityGroups": [{"GroupId": "sg-attached"}]}
        self.assertFalse(recorded_security_group_matches(instance, {"security_group_id": "sg-other"}))
        instance["SecurityGroups"].append({"GroupId": "sg-extra"})
        self.assertFalse(recorded_security_group_matches(instance, {"security_group_id": "sg-attached"}))

    def test_missing_recorded_security_group_fails_closed(self):
        instance = {"SecurityGroups": [{"GroupId": "sg-attached"}]}
        self.assertFalse(recorded_security_group_matches(instance, {}))


if __name__ == "__main__":
    unittest.main()