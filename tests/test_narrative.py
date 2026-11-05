import unittest

from cursor_timelog.narrative import cluster_beats, narrate


AUTH = [
    "Investigated an exception in the authentication module.",
    "Fixed a token validation issue.",
    "Updated error handling.",
    "Resolved another exception related to session expiration.",
    "Tested the authentication flow.",
]

WHOHASIT = [
    "Prepare list of custom crons, which are currently exist in this project, prepare it's code, cron time duration, short purpose.",
    "Thanks for the list, now how can we verify this cron in safe way so that no real action is performed and no email is sent to a real customer?",
    "I want to verify each cron first which does not send email and which is not sensitive.",
    "There is an issue with auto_renew_vendor_membership cron. It only updates status to 4 and does not renew.",
    "so what is the fix?",
    "revert changes from app/code/Webkul/Odoomagentomultishop/Model/ResourceModel/Multishop.php",
    "Please give me the m2 hotfix changes now, I reverted the direct changes.",
    "1 exception(s): Exception #0 (Exception): Warning: Undefined property: Zynovo\\Stripe\\Model\\Config::$secret",
]


class NarrativeTests(unittest.TestCase):
    def test_authentication_work_is_one_natural_summary(self):
        text = narrate(AUTH)
        self.assertIn("authentication module", text.lower())
        self.assertIn("token validation", text.lower())
        self.assertIn("session expiration", text.lower())
        self.assertIn("error handling", text.lower())
        self.assertIn("tested the authentication flow", text.lower())
        self.assertTrue(text.endswith("."))
        self.assertNotIn("I ", text)

    def test_same_tab_keeps_every_distinct_topic(self):
        groups = cluster_beats([{"request": item, "start": index, "end": index + 1} for index, item in enumerate(WHOHASIT)])
        summaries = [narrate([beat["request"] for beat in group]) for group in groups]
        blob = " ".join(summaries).lower()
        self.assertGreaterEqual(len(summaries), 3)
        self.assertIn("cron", blob)
        self.assertIn("membership renewal", blob)
        self.assertIn("multishop.php", blob)
        self.assertIn("stripe", blob)
        # The last exception must not be the only summary.
        self.assertGreater(len(summaries), 1)
        self.assertFalse(all("stripe" in item.lower() for item in summaries))


if __name__ == "__main__":
    unittest.main()
