# SPDX-License-Identifier: Apache-2.0
"""One price per AWS resource, in one place.

This module exists because of a measured defect, not a tidiness preference.
analyzers/waste.py priced an idle Application Load Balancer at $0.008/hr * 730 =
$5.84/month. cleanup/idle.py priced the identical resource at $16.20/month. Both
numbers shipped, and which one a customer saw depended only on which tool they
happened to call: get_idle_load_balancers, audit_aws_waste and `nable scan` took
the waste.py path, while list_idle_resources and run_full_cost_audit took
idle.py's. Two answers 2.8x apart for one load balancer in one session, and
waste_evidence labelled the low one MEASURED at high confidence.

idle.py was right, and waste.py's constant was not a typo but a category error:
$0.008 is the LCU-hour price and $0.0225 is the hourly base charge every ALB
pays whether or not it serves a single request. An idle load balancer has no
LCU usage by definition, so the LCU rate was precisely the wrong half of the
bill to charge for it.

The general shape, which is the point of this file: a duplicated constant is not
a bug until the copies drift, and nothing in a test suite notices drift between
two literals in two files. Detecting it needs a place where they cannot be two.
Prices that more than one module needs belong here.

Rates are us-east-1 on-demand list, the basis every estimate in nable is quoted
on. Regional variation is real but small for these fixed hourly charges, and a
per-region table would be a false precision on top of an estimate the customer
is told is an estimate.
"""
from __future__ import annotations

# AWS's own convention for a "month" in pricing examples: 365 * 24 / 12. Using
# 720 (30 days) instead understates every monthly figure by 1.4%, which is
# invisible per resource and material across a fleet.
HOURS_PER_MONTH = 730.0

# Elastic Load Balancing, hourly base charge. Application and Network load
# balancers are the same $0.0225; Classic is $0.025. These are the "exists at
# all" rates, exclusive of LCU/NLCU and data processing, which is the correct
# basis for an IDLE resource: capacity units are what it would cost if it were
# doing work, and the finding is that it is not.
ALB_HOURLY = 0.0225
NLB_HOURLY = 0.0225
CLB_HOURLY = 0.025

# Rounded to cents at definition, not at each use. An hourly rate times 730 lands
# on fractions of a cent (0.0225 * 730 = 16.425), and a caller that rounds for
# display then no longer equals the constant it came from. Every comparison
# downstream would need the same tolerance, and the first one that forgot would
# be an off-by-half-a-cent failure that looks like a real disagreement. Money is
# denominated in cents; the constant is too.
ALB_PER_MONTH = round(ALB_HOURLY * HOURS_PER_MONTH, 2)   # 16.43
NLB_PER_MONTH = round(NLB_HOURLY * HOURS_PER_MONTH, 2)   # 16.43
CLB_PER_MONTH = round(CLB_HOURLY * HOURS_PER_MONTH, 2)   # 18.25
