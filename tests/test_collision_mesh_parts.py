"""Topology preparation must retain geometry and expose every render part."""
from collision_editor.mesh_parts import (split_triangle_components,
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

