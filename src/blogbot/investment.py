"""Explicit investment routing; Wednesday never falls through to Friday/legacy KIS."""
from . import life_economics, weekly_policy

ROUTES = {weekly_policy.VERSION: weekly_policy, life_economics.VERSION: life_economics}


def request_contract(request, plan):
    mode = plan.get('investment_mode')
    if not mode:
        return not request.provenance.get('investment_mode')
    if mode == life_economics.VERSION:
        return life_economics.request_contract(request)
    return (mode == weekly_policy.VERSION and request.data.get('kind') == 'policy'
            and request.provenance.get('investment_mode') == mode
            and bool(request.provenance.get('policy_event_key')))


def post_contract(post, plan):
    mode = plan.get('investment_mode')
    if not mode:
        return not post.provenance.get('investment_mode')
    return mode in ROUTES and ROUTES[mode].post_contract(post, plan)


def _route(provenance, settings, category):
    from .research import ResearchRequired
    mode = provenance.get('investment_mode')
    expected = (getattr(settings, 'config', {}).get('daily_plan') or {}).get('investment_mode')
    if expected and (category != 'investment' or mode != expected):
        raise ResearchRequired('Investment route does not match current daily plan')
    if mode and mode not in ROUTES:
        raise ValueError('Unknown investment route')
    return ROUTES.get(mode)


def verify_evidence(settings, request):
    route = _route(request.provenance, settings, request.category)
    return route.verify_evidence(settings, request) if route else request


def refresh_request(settings, request):
    route = _route(request.provenance, settings, request.category)
    return route.refresh_request(settings, request) if route else request


def validate_current_post(settings, post):
    route = _route(post.provenance, settings, post.category)
    if route:
        route.validate_current_post(settings, post)


def revalidation_reason(plan):
    return ('life_economics_revalidation_required' if (plan or {}).get('investment_mode')
            == life_economics.VERSION else 'weekly_policy_revalidation_required')
