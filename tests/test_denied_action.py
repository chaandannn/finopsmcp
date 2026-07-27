"""When a tool fails on a permission gap, nable must name the exact missing IAM
action instead of swallowing it and letting the model guess 'maybe CloudWatch,
maybe region'. Guards the rightsizing-error regression."""
from finops.server import _denied_action


def test_extracts_the_iam_action():
    msg = ("User: arn:aws:iam::123456789012:user/Nable-User is not authorized to "
           "perform: rds:DescribeDBInstances because no identity-based policy allows "
           "the rds:DescribeDBInstances action")
    assert _denied_action(msg) == "rds:DescribeDBInstances"


def test_handles_unauthorized_operation_phrasing():
    msg = "An error occurred (UnauthorizedOperation) when calling DescribeDBClusters: ..."
    # No 'to perform:' marker, but it IS a permission error -> generic, not empty.
    assert _denied_action(msg) == "an AWS read action"


def test_empty_for_non_permission_errors():
    assert _denied_action("Connection timed out after 3s") == ""
    assert _denied_action("Throttling: Rate exceeded") == ""
    assert _denied_action("DataUnavailableException: not backfilled yet") == ""


def test_generic_when_access_denied_without_parseable_action():
    assert _denied_action("AccessDenied: opaque message") == "an AWS read action"
