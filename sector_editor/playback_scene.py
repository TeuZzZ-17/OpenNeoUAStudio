"""Place damage-transition effects at each sector cell without editing models."""

def sector_transition_steps(document, number, old_state, new_state):
    """All cells explode at their own position, with the engine's file-axis conversion."""
    sector = document.sdf.sectors[number]
    direction = 1 if new_state > old_state else -1
    steps = []
    for state in range(old_state, new_state, direction):
        effects = []
        for cell, bid in enumerate(sector.subsects):
            ox, oz = (0, 0) if sector.single else ((cell % 3 - 1) * 300, (1 - cell // 3) * 300)
            model = document.sdf.models[document.sdf.buildings[bid].state(state)]
            effects.extend((effect, x + ox, z - oz, height) for effect, x, z, height in model.effects)
        steps.append(effects)
    return steps
