from typing import Dict, Optional

STATE_SEARCH = "search_query"
STATE_MANUAL_TITLE = "manual_title"
STATE_MANUAL_SEASON = "manual_season"

def set_state(user_data: Dict, state: Optional[str]) -> None:
    if state:
        user_data["awaiting"] = state
    else:
        user_data.pop("awaiting", None)


def reset_flow_state(context) -> None:
    for key in [
        "awaiting",
        "pending_show",
        "pending_manual_type",
        "manual_title",
        "last_results",
        "results_list",
        "results_map",
        "results_page",
    ]:
        context.user_data.pop(key, None)
    for key in ["tdl_extra_flags", "download_dir", "season_hint", "active_selection", "selected_type"]:
        context.chat_data.pop(key, None)
