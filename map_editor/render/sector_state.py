"""Resolve runtime building sectors without changing the authored LDF grids."""

def sector_type(doc, lib, col, row):
    typ = int(str(doc.grids['type'][row][col]), 16)
    bid = int(str(doc.grids['blg'][row][col]), 16)
    building = lib.buildings.get(bid) if lib is not None and bid else None
    # LoadBlgMap constructs owned buildings using the prototype SecType.
    if building is not None and doc.grids['own'][row][col]:
        return building.sec_type
    return typ
