import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logic  # noqa: E402
import server  # noqa: E402


def rule(*conds):
    return {"all": [{"field": f, "op": o, "value": v} for f, o, v in conds]}


def field(key, ftype="text", required=False, show_if=None, options=None):
    return {"field_key": key, "field_type": ftype, "required": required, "show_if": show_if,
            "options": options}


def raw(fields, **extra):
    d = {"firstName": "Sam", "lastName": "Lee", "email": "sam@example.com", "consent": True,
         "language": "en", "fields": fields}
    d.update(extra)
    return d


class RuleCleaningTests(unittest.TestCase):
    def test_empty_means_always_shown(self):
        for v in (None, "", {}, [], {"all": []}):
            self.assertEqual(logic.clean_show_if(v), (None, None))

    def test_good_rule_is_kept(self):
        r, err = logic.clean_show_if(rule(("nps", "lte", 6), ("kind", "in", ["a", "b"])))
        self.assertIsNone(err)
        self.assertEqual(r["all"][0], {"field": "nps", "op": "lte", "value": "6"})
        self.assertEqual(r["all"][1]["value"], ["a", "b"])

    def test_bad_rules_are_refused(self):
        bad = [
            {"all": "x"}, {"any": []}, {"all": [5]},
            rule(("Bad Key", "eq", "1")), rule(("nps", "weird", "1")),
            rule(("nps", "eq", "")), rule(("nps", "gte", "abc")), rule(("nps", "in", [])),
            rule(("nps", "in", "a")), rule(("nps", "lte", "1e3")),
        ]
        for b in bad:
            self.assertIsNotNone(logic.clean_show_if(b)[1], b)

    def test_too_many_conditions(self):
        r = {"all": [{"field": "a", "op": "eq", "value": "1"}] * 11}
        self.assertIsNotNone(logic.clean_show_if(r)[1])


class VisibilityTests(unittest.TestCase):
    def test_nps_low_shows_followup(self):
        fields = [field("nps", "rating"), field("fix", show_if=rule(("nps", "lte", "6")))]
        self.assertIn("fix", logic.visible_keys(fields, {"nps": 5}))
        self.assertIn("fix", logic.visible_keys(fields, {"nps": "6"}))
        self.assertNotIn("fix", logic.visible_keys(fields, {"nps": 7}))

    def test_numeric_strings_work_for_at_least_and_at_most(self):
        fields = [field("n", "select"), field("hi", show_if=rule(("n", "gte", "9"))),
                  field("lo", show_if=rule(("n", "lte", "6")))]
        shown = logic.visible_keys(fields, {"n": "9"})
        self.assertIn("hi", shown)
        self.assertNotIn("lo", shown)
        shown = logic.visible_keys(fields, {"n": "10"})   # 10 > 9 as numbers, not as text
        self.assertIn("hi", shown)
        shown = logic.visible_keys(fields, {"n": "0"})
        self.assertIn("lo", shown)

    def test_is_is_not_one_of(self):
        fields = [field("c", "select"), field("a", show_if=rule(("c", "eq", "yes"))),
                  field("b", show_if=rule(("c", "neq", "yes"))),
                  field("d", show_if=rule(("c", "in", ["yes", "maybe"])))]
        self.assertEqual({"c", "a", "d"}, logic.visible_keys(fields, {"c": "yes"}))
        self.assertEqual({"c", "b"}, logic.visible_keys(fields, {"c": "no"}))

    def test_no_answer_means_no_match(self):
        fields = [field("c", "select"), field("b", show_if=rule(("c", "neq", "yes")))]
        self.assertEqual({"c"}, logic.visible_keys(fields, {}))
        self.assertEqual({"c"}, logic.visible_keys(fields, {"c": ""}))

    def test_and_needs_every_condition(self):
        fields = [field("a", "rating"), field("b", "rating"),
                  field("x", show_if=rule(("a", "lte", "3"), ("b", "gte", "4")))]
        self.assertIn("x", logic.visible_keys(fields, {"a": 2, "b": 5}))
        self.assertNotIn("x", logic.visible_keys(fields, {"a": 2, "b": 3}))
        self.assertNotIn("x", logic.visible_keys(fields, {"a": 4, "b": 5}))

    def test_chain_a_hidden_question_counts_as_unanswered(self):
        fields = [field("a", "select"), field("b", "text", show_if=rule(("a", "eq", "y"))),
                  field("c", "text", show_if=rule(("b", "eq", "hello")))]
        # b is hidden, so even a posted "hello" for it must not open c
        self.assertEqual({"a"}, logic.visible_keys(fields, {"a": "n", "b": "hello"}))
        self.assertEqual({"a", "b", "c"}, logic.visible_keys(fields, {"a": "y", "b": "hello"}))

    def test_rule_pointing_forward_or_at_nothing_stays_hidden(self):
        fields = [field("x", show_if=rule(("later", "eq", "1"))), field("later", "text"),
                  field("y", show_if=rule(("gone", "eq", "1")))]
        self.assertEqual({"later"}, logic.visible_keys(fields, {"later": "1"}))

    def test_checkbox(self):
        fields = [field("t", "checkbox"), field("x", show_if=rule(("t", "eq", "true")))]
        self.assertIn("x", logic.visible_keys(fields, {"t": True}))
        self.assertNotIn("x", logic.visible_keys(fields, {"t": False}))


class SubmitChecksTests(unittest.TestCase):
    FIELDS = [
        field("nps", "rating", required=True),
        field("fix", "text", required=True, show_if=rule(("nps", "lte", "3"))),
        field("why", "select", show_if=rule(("nps", "gte", "4")),
              options=[{"value": "a"}, {"value": "b"}]),
    ]

    def test_consent_required_only_with_agree_box(self):
        d = raw({"nps": 5}); d["consent"] = False
        rec, err = server.validate_submission(d, self.FIELDS)
        self.assertEqual(err, "consent not given")
        rec, err = server.validate_submission(d, self.FIELDS, require_consent=False)
        self.assertIsNone(err)
        self.assertFalse(rec["consent"])

    def test_hidden_required_question_is_not_required(self):
        rec, err = server.validate_submission(raw({"nps": 5}), self.FIELDS)
        self.assertIsNone(err)
        self.assertEqual(rec["extra"], {"nps": 5})

    def test_visible_required_question_is_required(self):
        rec, err = server.validate_submission(raw({"nps": 2}), self.FIELDS)
        self.assertIsNotNone(err)
        rec, err = server.validate_submission(raw({"nps": 2, "fix": "Faster"}), self.FIELDS)
        self.assertIsNone(err)
        self.assertEqual(rec["extra"], {"nps": 2, "fix": "Faster"})

    def test_answer_to_hidden_question_is_dropped(self):
        # posted by hand: "fix" is hidden at nps 5, "why" is hidden at nps 2
        rec, err = server.validate_submission(raw({"nps": 5, "fix": "sneaky"}), self.FIELDS)
        self.assertIsNone(err)
        self.assertNotIn("fix", rec["extra"])
        rec, err = server.validate_submission(raw({"nps": 2, "fix": "ok text", "why": "a"}), self.FIELDS)
        self.assertIsNone(err)
        self.assertNotIn("why", rec["extra"])

    def test_hidden_question_with_garbage_is_not_an_error(self):
        rec, err = server.validate_submission(raw({"nps": 2, "fix": "fine text", "why": "not-an-option"}),
                                              self.FIELDS)
        self.assertIsNone(err)
        self.assertEqual(rec["extra"], {"nps": 2, "fix": "fine text"})

    def test_old_forms_without_rules_behave_as_before(self):
        fields = [field("a", "text", required=True)]
        _rec, err = server.validate_submission(raw({}), fields)
        self.assertIsNotNone(err)
        rec, err = server.validate_submission(raw({"a": "hi"}), fields)
        self.assertEqual(rec["extra"], {"a": "hi"})
        self.assertEqual(rec["hidden"], {})


class HiddenFieldTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(logic.parse_hidden_names("branch, source staff"), (["branch", "source", "staff"], None))
        self.assertEqual(logic.parse_hidden_names(""), ([], None))
        self.assertEqual(logic.parse_hidden_names("a,a,b")[0], ["a", "b"])
        self.assertIsNotNone(logic.parse_hidden_names("bad name!")[1])
        self.assertIsNotNone(logic.parse_hidden_names(",".join("n%d" % i for i in range(11)))[1])

    def test_only_listed_names_are_saved_and_capped(self):
        out = logic.clean_hidden_values({"branch": "dubai", "evil": "x", "source": "s" * 300,
                                         "staff": "a\nb\x00c", "n": 5}, ["branch", "source", "staff", "n"])
        self.assertEqual(out["branch"], "dubai")
        self.assertNotIn("evil", out)
        self.assertEqual(len(out["source"]), 100)
        self.assertEqual(out["staff"], "abc")
        self.assertNotIn("n", out)

    def test_submission_keeps_only_listed_hidden_values(self):
        rec, err = server.validate_submission(raw({}, hidden={"branch": "dubai", "x": "y"}), [],
                                              hidden_names=["branch"])
        self.assertIsNone(err)
        self.assertEqual(rec["hidden"], {"branch": "dubai"})
        rec, _ = server.validate_submission(raw({}, hidden={"branch": "dubai"}), [])
        self.assertEqual(rec["hidden"], {})


if __name__ == "__main__":
    unittest.main()
