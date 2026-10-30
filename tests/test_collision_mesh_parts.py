"""Topology preparation must retain geometry and expose every render part."""
from entity_designer.mesh_parts import (split_triangle_components,
    prepare_component_mesh, topology_diagnostic, _triangulate_loop)


def tetrahedron():
    a,b,c,d=(0.,0.,0.),(1.,0.,0.),(0.,1.,0.),(0.,0.,1.)
    return [(a,c,b),(a,b,d),(a,d,c),(b,c,d)]


def test_components_retain_flat_effect_and_stable_ids():
    solid=tetrahedron()
    sheet=[((5.,0.,0.),(6.,0.,0.),(5.,1.,0.))]
    parts=split_triangle_components([('body',solid+sheet)])
    assert len(parts)==2
    assert sum(len(c.triangles) for c in parts)==5
    assert sum(c.planar for c in parts)==1
    assert sum(c.has_volume for c in parts)==1
    reverse=split_triangle_components([('body',list(reversed(solid+sheet)))])
    assert [c.component_id for c in reverse]==[c.component_id for c in parts]
    assert all('triangles' in topology_diagnostic(c) for c in parts)


def test_existing_t_junction_is_split_without_inventing_surface():
    solid=tetrahedron()
    a,c,b=solid[0]; midpoint=tuple((a[i]+b[i])/2 for i in range(3))
    raw=[(a,c,midpoint),(midpoint,c,b),*solid[1:]]
    assert split_triangle_components([('body',raw)])[0].boundary_edges==3
    prepared,warnings=prepare_component_mesh(raw)
    assert any('T-junctions' in w for w in warnings)
    part=split_triangle_components([('body',prepared)])[0]
    assert part.boundary_edges==0 and part.non_manifold_edges==0
    # Preparation uses only vertices from the old surface and face centres.
    assert all(all(-1e-10<=n<=1+1e-10 for n in p) and sum(p)<=1+1e-10
               for t in prepared for p in t)


def test_only_simple_planar_hole_gets_a_cap():
    raw=tetrahedron()[:-1]
    prepared,warnings=prepare_component_mesh(raw)
    assert len(prepared)==4
    assert split_triangle_components([('body',prepared)])[0].boundary_edges==0
    assert any('Closed 1 simple planar boundary' in w for w in warnings)
    crossing=[(0.,0.,0.),(1.,1.,0.),(0.,1.,0.),(1.,0.,0.)]
    assert _triangulate_loop(crossing,1e-8) is None
    nonplanar=[(0.,0.,0.),(1.,0.,0.),(1.,1.,1.),(0.,1.,0.)]
    assert _triangulate_loop(nonplanar,1e-8) is None


def test_planar_component_is_never_silently_thickened_or_deleted():
    raw=[((0.,0.,0.),(1.,0.,0.),(0.,1.,0.))]
    prepared,warnings=prepare_component_mesh(raw)
    assert prepared==tuple(raw)
    assert warnings and 'planar' in warnings[0]


def test_inconsistent_source_winding_is_oriented_for_closed_body():
    raw=tetrahedron(); raw[1]=tuple(reversed(raw[1]))
    prepared,_=prepare_component_mesh(raw)
    edges={}
    for triangle in prepared:
        for a,b in zip(triangle,triangle[1:]+triangle[:1]):
            edges.setdefault(tuple(sorted((a,b))),[]).append(a<b)
    assert all(sorted(directions)==[False,True] for directions in edges.values())



def box_with_tower():
    """A 4x4x1 box with a 2x2x2 tower: one connected piece, two parts."""
    def quad(a, b, c, d):
        return [(a, b, c), (a, c, d)]
    o = [(0., 0., 1.), (4., 0., 1.), (4., 4., 1.), (0., 4., 1.)]
    i = [(1., 1., 1.), (3., 1., 1.), (3., 3., 1.), (1., 3., 1.)]
    t = [(x, y, 3.) for x, y, _ in i]
    b = [(x, y, 0.) for x, y, _ in o]
    box = quad(b[0], b[3], b[2], b[1])
    for k in range(4):
        n = (k + 1) % 4
        box += quad(b[k], b[n], o[n], o[k])
        box += quad(o[k], o[n], i[n], i[k])
    tower = quad(t[0], t[1], t[2], t[3])
    for k in range(4):
        n = (k + 1) % 4
        tower += quad(i[k], i[n], t[n], t[k])
    return box, tower


def test_split_regions_separates_a_part_attached_through_folds():
    box, tower = box_with_tower()
    whole = split_triangle_components([('body', box + tower)])
    parts = split_triangle_components([('body', box + tower)], split_regions=True)
    assert len(whole) == 1 and len(parts) == 2
    assert sorted(len(c.triangles) for c in parts) == [len(tower), len(box)]
    assert {c.group_id for c in parts} == {whole[0].component_id}
    assert sum(len(c.triangles) for c in parts) == len(box + tower)


def test_split_regions_keeps_a_convex_piece_and_its_old_id():
    whole = split_triangle_components([('body', tetrahedron())])
    parts = split_triangle_components([('body', tetrahedron())], split_regions=True)
    assert [c.component_id for c in parts] == [c.component_id for c in whole]
    assert parts[0].group_id == parts[0].component_id
