import logging
from uuid import UUID

logger = logging.getLogger(__name__)

ITEM_ROLE: dict[str, str] = {
    "shirt": "base_top",
    "t-shirt": "base_top",
    "blouse": "base_top",
    "polo": "base_top",
    "tank-top": "base_top",
    "top": "base_top",
    "sweater": "base_top",
    "pants": "bottom",
    "jeans": "bottom",
    "shorts": "bottom",
    "skirt": "bottom",
    "dress": "full_body",
    "jumpsuit": "full_body",
    "cardigan": "mid_layer",
    "vest": "mid_layer",
    "jacket": "outer_layer",
    "blazer": "outer_layer",
    "coat": "outer_layer",
    "hoodie": "outer_layer",
    "shoes": "footwear",
    "sneakers": "footwear",
    "boots": "footwear",
    "sandals": "footwear",
    "socks": "socks",
    "tie": "neckwear",
    "hat": "accessory",
    "scarf": "accessory",
    "belt": "accessory",
    "bag": "accessory",
    "accessories": "accessory",
}


def deduplicate_by_body_slot(
    item_ids: list[UUID],
    item_type_map: dict[UUID, str],
    protected_ids: set[UUID] | None = None,
) -> list[UUID]:
    """Drop items that would duplicate a body slot.

    ``protected_ids`` (e.g. mandatory/force-included items) are never removed:
    they always survive and claim their body slot, so a conflicting *non*-protected
    item is dropped in their favour instead. Two protected items that conflict are
    both kept, since the caller explicitly required them.
    """
    protected_ids = protected_ids or set()

    def role_of(iid: UUID) -> str | None:
        return ITEM_ROLE.get(item_type_map.get(iid, ""))

    # Pre-compute the influence of protected items so non-protected items can be
    # resolved against them regardless of input ordering.
    protected_roles: dict[str, UUID] = {}
    protected_has_full_body = False
    protected_has_base_or_bottom = False
    for iid in item_ids:
        if iid not in protected_ids:
            continue
        role = role_of(iid)
        if not role or role == "accessory":
            continue
        protected_roles.setdefault(role, iid)
        if role == "full_body":
            protected_has_full_body = True
        if role in ("base_top", "bottom"):
            protected_has_base_or_bottom = True

    # A non-protected full_body item cannot be kept when a protected top/bottom
    # exists (we would have to drop the protected item to honour it).
    nonprotected_full_body_exists = any(
        role_of(iid) == "full_body" for iid in item_ids if iid not in protected_ids
    )
    will_keep_full_body = protected_has_full_body or (
        nonprotected_full_body_exists and not protected_has_base_or_bottom
    )

    seen_roles: dict[str, UUID] = dict(protected_roles)
    result: list[UUID] = []
    for iid in item_ids:
        item_type = item_type_map.get(iid, "")
        role = ITEM_ROLE.get(item_type)

        if iid in protected_ids:
            # Mandatory item: always keep, never dropped by dedup.
            result.append(iid)
            continue

        if not role:
            result.append(iid)
            continue
        if role == "accessory":
            result.append(iid)
            continue
        if role == "full_body" and protected_has_base_or_bottom:
            logger.warning(
                f"Removing {item_type} item {iid}: mandatory top/bottom item present"
            )
            continue
        if role in ("base_top", "bottom") and will_keep_full_body:
            logger.warning(f"Removing {item_type} item {iid}: full_body item present")
            continue
        if role in seen_roles:
            logger.warning(
                f"Removing duplicate {role} item {iid} ({item_type}): "
                f"role already filled by {seen_roles[role]}"
            )
            continue
        seen_roles[role] = iid
        result.append(iid)
    return result


_CANONICAL_ROLE_ORDER = [
    "full_body",
    "base_top",
    "mid_layer",
    "outer_layer",
    "bottom",
    "footwear",
    "socks",
    "neckwear",
    "accessory",
]

_ROLE_SORT_INDEX: dict[str, int] = {role: idx for idx, role in enumerate(_CANONICAL_ROLE_ORDER)}


def canonical_item_order(item_ids: list[UUID], item_type_map: dict[UUID, str]) -> list[UUID]:
    original_positions = {iid: idx for idx, iid in enumerate(item_ids)}

    def sort_key(item_id: UUID) -> tuple[int, int]:
        item_type = item_type_map.get(item_id, "")
        role = ITEM_ROLE.get(item_type)
        role_idx = (
            _ROLE_SORT_INDEX.get(role, len(_CANONICAL_ROLE_ORDER))
            if role
            else len(_CANONICAL_ROLE_ORDER)
        )
        return (role_idx, original_positions[item_id])

    return sorted(item_ids, key=sort_key)
