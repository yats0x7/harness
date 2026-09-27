from trojan.issue import Issue, load_issue, task_spec


def test_free_form_feature_task_extracts_type_acceptance_and_constraints():
    spec = task_spec("""Build a CSV export command.

Type: feature
Acceptance criteria:
- Include a header row
- Preserve quoted commas

Constraints:
- Keep the existing CLI compatible
""")
    assert spec.kind == "feature"
    assert spec.acceptance == ("Include a header row", "Preserve quoted commas")
    assert spec.constraints == ("Keep the existing CLI compatible",)


def test_bug_reports_keep_the_legacy_bugfix_default():
    issue = load_issue("mean() returns the wrong value for a pair")
    assert isinstance(issue, Issue)
    assert issue.kind == "bugfix"


def test_task_alias_is_accepted_by_the_loader():
    issue = load_issue("Type: refactor\n\nSimplify the parser without changing its public API.")
    assert issue.kind == "refactor"
