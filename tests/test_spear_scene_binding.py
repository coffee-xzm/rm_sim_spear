import unittest

from rm_sim_spear.spear_scene_binding import (
    _SceneActorNotFound,
    _actor_attachment_path,
    _derive_runtime_asset_path,
    _ensure_actor_root,
    _find_actor,
    _find_or_spawn_anchor,
    _get_attached_actors,
    _resolve_runtime_children,
    _spawn_runtime_static_mesh_actor,
    apply_scene_binding_plan,
)


class _FakeActor:
    def K2_GetRootComponent(self, *, as_handle):
        self.as_handle = as_handle
        return 0


class _FakeUnrealService:
    def __init__(self):
        self.created = []

    def create_scene_component_for_actor(self, **kwargs):
        self.created.append(kwargs)
        return "created-root"

    def get_component_by_name(self, **kwargs):
        return None


class _FakeGame:
    def __init__(self):
        self.unreal_service = _FakeUnrealService()


class _FakePathActor:
    def __init__(self, stable_name, parent_handle=0):
        self.stable_name = stable_name
        self.parent_handle = parent_handle

    def GetAttachParentActor(self, *, as_handle):
        assert as_handle
        return self.parent_handle


class _FakeActorListService:
    def __init__(self, actors):
        self.actors = actors

    def find_actors_as_dict(self):
        return {index: actor for index, actor in enumerate(self.actors)}


class _FakePathService:
    def __init__(self, actors):
        self.actors = actors

    def get_stable_name_for_actor(self, *, actor):
        return actor.stable_name

    def to_handle_or_unreal_object(self, *, obj, as_unreal_object):
        assert as_unreal_object
        return self.actors[obj]


class _FakePathGame:
    def __init__(self, actors):
        self.unreal_service = _FakePathService(actors)


class _FakeActorListGame:
    def __init__(self, actors):
        self.unreal_service = _FakeActorListService(actors)


class SceneBindingRootTests(unittest.TestCase):
    def test_empty_root_handle_creates_scene_component_without_object_conversion(self):
        game = _FakeGame()
        actor = _FakeActor()

        root = _ensure_actor_root(game, actor, "RMSimRoot")

        self.assertEqual(root, "created-root")
        self.assertTrue(actor.as_handle)
        self.assertEqual(game.unreal_service.created, [{
            "uclass": "USceneComponent",
            "owner": actor,
            "scene_component_name": "RMSimRoot",
        }])

    def test_attachment_path_includes_parent_actors(self):
        parent = _FakePathActor("Scene")
        child = _FakePathActor("armor", parent_handle=1)
        game = _FakePathGame({1: parent})

        self.assertEqual(_actor_attachment_path(game, child), "Scene/armor")

    def test_attached_actor_lookup_returns_only_direct_children(self):
        parent = _FakePathActor("parent")
        parent.uobject = 1
        child = _FakePathActor("child", parent_handle=1)
        child.uobject = 2
        grandchild = _FakePathActor("grandchild", parent_handle=2)
        grandchild.uobject = 3
        game = _FakeActorListGame([parent, child, grandchild])

        self.assertEqual(_get_attached_actors(game, parent), [child])

    def test_runtime_hierarchy_replaces_stale_importer_child_names(self):
        parent = _FakePathActor("body")
        parent.uobject = 1
        merged = _FakePathActor("merged-mesh", parent_handle=1)
        merged.uobject = 2
        unrelated = _FakePathActor("old-detached-mesh")
        unrelated.uobject = 3
        game = _FakeActorListGame([parent, merged, unrelated])

        self.assertEqual(
            _resolve_runtime_children(game, parent, ["deleted-importer-node"]),
            [merged],
        )

    def test_merged_actor_needs_no_planned_children(self):
        merged = _FakePathActor("body")
        merged.uobject = 1
        game = _FakeActorListGame([merged])

        self.assertEqual(
            _resolve_runtime_children(game, merged, ["deleted-importer-node"]),
            [],
        )


if __name__ == "__main__":
    unittest.main()


class _FakeMeshComponent:
    def __init__(self):
        self.mobility = None
        self.mesh = None

    def SetMobility(self, *, NewMobility):
        self.mobility = NewMobility

    def SetStaticMesh(self, *, NewMesh):
        self.mesh = NewMesh


class _FakeRuntimeMeshService:
    def __init__(self, mesh_available=True, components=1):
        self.mesh_available = mesh_available
        self.components = [_FakeMeshComponent() for _ in range(components)]
        self.loaded = []
        self.spawned = []
        self.stable_names = []

    def load_object(self, *, uclass, name, as_unreal_object):
        self.loaded.append((uclass, name))
        return f"mesh:{name}" if self.mesh_available else None

    def load_class(self, *, uclass, name):
        self.loaded_classes = getattr(self, "loaded_classes", [])
        self.loaded_classes.append((uclass, name))
        return f"class:{name}"

    def spawn_actor(self, *, uclass, spawn_parameters, as_unreal_object):
        self.spawned.append((uclass, spawn_parameters))
        return f"actor:{uclass}"

    def get_components_by_class(self, *, actor, uclass):
        return self.components

    def set_stable_name_for_actor(self, *, actor, stable_name):
        self.stable_names.append((actor, stable_name))


class _FakeRuntimeMeshGame:
    def __init__(self, service):
        self.unreal_service = service


class RuntimeVisualFallbackTests(unittest.TestCase):
    """A standalone world has no editor-time merged actor; the asset is loaded."""

    def test_asset_path_is_derived_from_the_merged_actor_name(self):
        self.assertEqual(
            _derive_runtime_asset_path({"actor_name": "SM_MERGED_blender_for_ue5_node_22"}),
            "/Game/SM_MERGED_blender_for_ue5_node_22",
        )

    def test_explicit_asset_path_wins(self):
        self.assertEqual(
            _derive_runtime_asset_path({
                "actor_name": "SM_MERGED_x",
                "asset_path": "/Game/Robots/SM_MERGED_x",
            }),
            "/Game/Robots/SM_MERGED_x",
        )

    def test_no_name_and_no_path_means_no_fallback(self):
        self.assertIsNone(_derive_runtime_asset_path({}))

    def test_spawn_loads_the_mesh_and_makes_it_movable(self):
        service = _FakeRuntimeMeshService()
        actor = _spawn_runtime_static_mesh_actor(
            _FakeRuntimeMeshGame(service), "/Game/SM_MERGED_x", "RMSim/robot_visual")

        self.assertEqual(service.loaded, [("UStaticMesh", "/Game/SM_MERGED_x")])
        self.assertEqual(
            service.loaded_classes,
            [("AActor", "/Script/Engine.StaticMeshActor")],
        )
        self.assertEqual(service.spawned[0][0], "class:/Script/Engine.StaticMeshActor")
        self.assertEqual(service.spawned[0][1], {"ObjectFlags": ["RF_Transient"]})
        self.assertEqual(service.components[0].mobility, "Movable")
        self.assertEqual(service.components[0].mesh, "mesh:/Game/SM_MERGED_x")
        self.assertEqual(service.stable_names, [(actor, "RMSim/robot_visual")])

    def test_missing_asset_is_an_error_not_a_silent_empty_robot(self):
        service = _FakeRuntimeMeshService(mesh_available=False)
        with self.assertRaises(RuntimeError):
            _spawn_runtime_static_mesh_actor(
                _FakeRuntimeMeshGame(service), "/Game/SM_MERGED_x", "RMSim/robot_visual")

    def test_actor_without_mesh_component_is_an_error(self):
        service = _FakeRuntimeMeshService(components=0)
        with self.assertRaises(RuntimeError):
            _spawn_runtime_static_mesh_actor(
                _FakeRuntimeMeshGame(service), "/Game/SM_MERGED_x", "RMSim/robot_visual")


class _FakeBindingComponent:
    """UStaticMeshComponent double: enough for cloning and mesh counting."""

    def __init__(self):
        self.mobility = None
        self.mesh = None

    def SetMobility(self, *, NewMobility):
        self.mobility = NewMobility

    def SetStaticMesh(self, *, NewMesh):
        self.mesh = NewMesh

    def SetMaterial(self, *, ElementIndex, Material):
        self.material = (ElementIndex, Material)

    def get_property_value(self, *, property_name, as_unreal_object=False, as_handle=False):
        assert property_name == "StaticMesh"
        return self.mesh

    def call(self, *, function_name, args=None, as_value=False, as_unreal_object=False):
        # Zero material slots keeps _copy_static_mesh_components a no-op there.
        assert function_name == "GetNumMaterials"
        return 0


class _FakeBindingActor:
    """Actor double exposing just the UE surface the binder uses."""

    def __init__(self, uobject, stable_name, *, parent=None, uclass="AActor",
                 mesh_components=0):
        self.uobject = uobject
        self.stable_name = stable_name
        self.parent = parent
        self.uclass = uclass
        self.root_handle = uobject
        self.components = [_FakeBindingComponent() for _ in range(mesh_components)]
        self.mobility = None
        self.hidden = False
        self.attached = []
        self.reset_count = 0
        self.scale = {"X": 1.0, "Y": 1.0, "Z": 1.0}

    def GetAttachParentActor(self, *, as_handle):
        assert as_handle
        return self.parent.uobject if self.parent is not None else 0

    def get_property_value(self, *, property_name, as_handle=False, as_unreal_object=False):
        assert property_name == "RootComponent"
        assert as_handle
        return self.root_handle

    def SetMobility(self, *, NewMobility):
        self.mobility = NewMobility

    def ResetRelativeTransform(self):
        self.reset_count += 1

    def SetActorHiddenInGame(self, *, bNewHidden):
        self.hidden = bNewHidden

    def K2_GetActorLocation(self):
        return {"X": 0.0, "Y": 0.0, "Z": 0.0}

    def K2_GetActorRotation(self):
        return {"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0}

    def GetActorScale3D(self):
        return dict(self.scale)

    def SetActorScale3D(self, *, NewScale3D):
        self.scale = dict(NewScale3D)

    def call(self, *, function_name, args=None, as_value=False, as_unreal_object=False):
        assert function_name == "K2_AttachToActor"
        # Mirror the engine: attaching really reparents, so later hierarchy
        # scans (mesh counting) see the same tree the engine would.
        self.parent = args["ParentActor"]
        self.attached.append(args)
        return None


class _FakeBindingService:
    """Unreal service double covering a whole binding transaction."""

    def __init__(self, actors, *, tags=None, tag_api=True):
        self.actors = list(actors)
        self.tags = dict(tags or {})
        self.tag_api = tag_api
        self.by_name = {actor.stable_name: actor for actor in self.actors}
        self.by_uobject = {actor.uobject: actor for actor in self.actors}
        # A spawned actor gets its class's component count, like UE does with
        # the class default object.
        self.component_counts = {}
        for actor in self.actors:
            self.component_counts.setdefault(actor.uclass, len(actor.components))
        self.name_lookups = []
        self.tag_lookups = []
        self.spawned = []
        self.stable_names = []
        self.next_uobject = max(self.by_uobject) + 1

    def find_actors_by_name_as_dict(self, *, actor_name, uclass):
        self.name_lookups.append(actor_name)
        actor = self.by_name.get(actor_name)
        return {actor.uobject: actor} if actor is not None else {}

    def find_actors_by_tag_as_dict(self, *, tag, uclass):
        if not self.tag_api:
            raise AttributeError("test double without a tag API")
        assert uclass == "AActor"
        self.tag_lookups.append(tag)
        return {actor.uobject: actor for actor in self.tags.get(tag, ())}

    def find_actors_as_dict(self, include_unreal_name=False, as_handle=None):
        # The binder falls back to the plain map when the unreal-name variant
        # is unavailable, as on older SPEAR wrappers.
        if include_unreal_name:
            raise TypeError("include_unreal_name is not supported")
        return {actor.stable_name: actor for actor in self.actors}

    def get_stable_name_for_actor(self, *, actor):
        return actor.stable_name

    def to_handle_or_unreal_object(self, *, obj, as_unreal_object):
        assert as_unreal_object
        if hasattr(obj, "uobject"):
            return obj
        return self.by_uobject.get(obj)

    def create_scene_component_for_actor(self, *, uclass, owner, scene_component_name):
        owner.created_root = scene_component_name
        return owner

    def get_components_by_class(self, *, actor, uclass, include_from_child_actors=False):
        assert uclass == "UStaticMeshComponent"
        return list(actor.components)

    def spawn_actor(self, *, uclass, spawn_parameters=None, location=None, rotation=None,
                    as_unreal_object=False):
        actor = _FakeBindingActor(
            self.next_uobject,
            f"spawned_{self.next_uobject}",
            uclass=uclass,
            mesh_components=self.component_counts.get(uclass, 0),
        )
        self.next_uobject += 1
        self.actors.append(actor)
        self.by_name[actor.stable_name] = actor
        self.by_uobject[actor.uobject] = actor
        self.spawned.append(actor)
        return actor

    def set_stable_name_for_actor(self, *, actor, stable_name):
        self.by_name.pop(actor.stable_name, None)
        actor.stable_name = stable_name
        self.by_name[stable_name] = actor
        self.stable_names.append((actor, stable_name))


class _FakeBindingGame:
    def __init__(self, service):
        self.unreal_service = service


def _link_hierarchy_game(*, link_meshes=1, with_armor=True, tag_api=True):
    """A standalone-style level: importer actors with opaque UObject names.

    The level also holds the editor-time merged robot visual that the default
    single-mesh path would bind, so tests can prove link_hierarchy mode never
    looks it up.
    """
    base_link = _FakeBindingActor(
        1, "StaticMeshActor_998", uclass="StaticMeshActor", mesh_components=link_meshes)
    link_1 = _FakeBindingActor(
        2, "StaticMeshActor_999", uclass="StaticMeshActor", mesh_components=link_meshes)
    merged = _FakeBindingActor(
        3, "SM_MERGED_blender_for_ue5_node_22", uclass="StaticMeshActor",
        mesh_components=7)
    actors = [base_link, link_1, merged]
    if with_armor:
        template = _FakeBindingActor(
            4, "armor_tune_node_4", uclass="StaticMeshActor", mesh_components=1)
        child = _FakeBindingActor(
            5, "armor_tune_node_0", parent=template, uclass="StaticMeshActor",
            mesh_components=1)
        actors += [template, child]
    tags = {
        "MuJoCoBody=base_link": [base_link],
        "MuJoCoBody=link_1": [link_1],
    }
    service = _FakeBindingService(actors, tags=tags, tag_api=tag_api)
    return _FakeBindingGame(service), service, base_link, link_1


def _link_hierarchy_plan(*, links=("base_link", "link_1"), armor=True):
    plan = {
        # A real plan also carries the merged actor's name; link_hierarchy mode
        # must ignore every one of these merged-visual fields.
        "robot_visual": {
            "link": "base_link",
            "actor_name": "SM_MERGED_blender_for_ue5_node_22",
            "actor_path": "Scene/body",
            "runtime_actor_name": "body",
            "mode": "link_hierarchy",
            "link_actor_tag": "MuJoCoBody={link}",
        },
        "links": [
            {
                "name": name,
                "parent": None,
                "joint": None,
                "origin_ue_cm": [0.0, 0.0, 0.0],
                "origin_rpy_rad": [0.0, 0.0, 0.0],
            }
            for name in links
        ],
        "armor": [],
        "camera": None,
        "imu": {
            "parent": "cam_link_8",
            "name": "imu_marker",
            "relative_position_m": [0.0, 0.0, 0.0],
            "relative_rpy_rad": [0.0, 0.0, 0.0],
        },
    }
    if armor:
        plan["armor"] = [{
            "link": "link_1",
            "actor_name": "armor_1",
            "actor_path": "Scene/armor",
            "material_slot": 0,
            "material_asset": None,
            "clone_per_link": True,
            "clone_name": "RMSim/armor_1",
            "hide_template": True,
            "runtime_actor_name": "armor_tune_node_4",
            "runtime_child_names": ["armor_tune_node_0"],
        }]
    return plan


class LinkHierarchyBindingTests(unittest.TestCase):
    """``robot_visual.mode == "link_hierarchy"`` binds one actor per URDF link."""

    def test_tagged_link_actors_are_bound_and_armor_attaches_to_them(self):
        game, service, base_link, link_1 = _link_hierarchy_game()
        diagnostics = {}

        link_actors = apply_scene_binding_plan(
            game, _link_hierarchy_plan(), binding_diagnostics=diagnostics)

        # Every planned link is driven, even the ones with no armor or camera.
        self.assertEqual(set(link_actors), {"base_link", "link_1"})
        self.assertIs(link_actors["base_link"], base_link)
        self.assertIs(link_actors["link_1"], link_1)
        self.assertEqual(
            service.tag_lookups, ["MuJoCoBody=base_link", "MuJoCoBody=link_1"])
        # Imported level actors arrive with static mobility, but the pose loop
        # moves each anchor every frame.
        self.assertEqual(base_link.mobility, "Movable")
        self.assertEqual(link_1.mobility, "Movable")

        self.assertEqual(diagnostics["robot_visual_source"], "imported_link_hierarchy")
        self.assertIsNone(diagnostics["robot_visual_actor"])
        self.assertEqual(diagnostics["robot_visual_mesh_components"], 2)

        # The merged editor-time visual is neither looked up nor attached.
        merged = service.by_name["SM_MERGED_blender_for_ue5_node_22"]
        self.assertNotIn("SM_MERGED_blender_for_ue5_node_22", service.name_lookups)
        self.assertNotIn("Scene/body", service.name_lookups)
        self.assertIsNone(merged.parent)

        # Armor is still cloned from the aggregate template and attached to the
        # already bound link anchor instead of a second, empty one.
        clone = diagnostics["armor_actors"][0]
        self.assertEqual(clone.stable_name, "RMSim/armor_1")
        self.assertIs(clone.parent, link_1)
        self.assertEqual(diagnostics["armor_clone_mesh_components"], [2])
        self.assertTrue(service.by_name["armor_tune_node_4"].hidden)

    def test_link_without_an_imported_actor_still_gets_a_spawned_anchor(self):
        game, service, _, _ = _link_hierarchy_game(with_armor=False)

        link_actors = apply_scene_binding_plan(
            game, _link_hierarchy_plan(links=("base_link", "link_1", "link_7"), armor=False))

        spawned = service.spawned[0]
        self.assertEqual(set(link_actors), {"base_link", "link_1", "link_7"})
        self.assertIs(link_actors["link_7"], spawned)
        self.assertEqual(service.stable_names, [(spawned, "link_7")])

    def test_missing_visuals_are_named_in_the_error(self):
        game, service, _, _ = _link_hierarchy_game(link_meshes=0, with_armor=False)

        with self.assertRaises(RuntimeError) as context:
            apply_scene_binding_plan(game, _link_hierarchy_plan(armor=False))

        message = str(context.exception)
        self.assertIn("UStaticMeshComponent", message)
        self.assertIn("base_link", message)
        self.assertIn("link_1", message)
        self.assertEqual(
            service.tag_lookups, ["MuJoCoBody=base_link", "MuJoCoBody=link_1"])

    def test_single_mesh_mode_is_unchanged_when_links_and_tags_exist(self):
        merged = _FakeBindingActor(
            1, "SM_MERGED_blender_for_ue5_node_22", uclass="StaticMeshActor",
            mesh_components=3)
        tagged = _FakeBindingActor(
            2, "tagged_visual", uclass="StaticMeshActor", mesh_components=1)
        service = _FakeBindingService(
            [merged, tagged], tags={"MuJoCoBody=base_link": [tagged]})
        plan = _link_hierarchy_plan()          # links list present, but no mode key
        plan["robot_visual"] = {
            "link": "base_link",
            "actor_name": "SM_MERGED_blender_for_ue5_node_22",
            "actor_path": None,
            "reset_relative_transform": True,
        }
        plan["armor"] = []
        diagnostics = {}
        game = _FakeBindingGame(service)

        link_actors = apply_scene_binding_plan(game, plan, binding_diagnostics=diagnostics)

        # The default path ignores both the link list and the tag API.
        self.assertEqual(set(link_actors), {"base_link"})
        self.assertEqual(service.tag_lookups, [])
        self.assertIsNot(link_actors["base_link"], tagged)
        self.assertIs(diagnostics["robot_visual_actor"], merged)
        self.assertEqual(diagnostics["robot_visual_source"], "scene_actor_override")
        self.assertEqual(diagnostics["robot_visual_mesh_components"], 3)
        self.assertIs(merged.parent, link_actors["base_link"])
        self.assertEqual(merged.reset_count, 1)
        self.assertEqual(game.unreal_service.spawned, [link_actors["base_link"]])


class TagLookupTests(unittest.TestCase):
    """Tag addressing resolves importer actors and refuses ambiguity."""

    def test_single_tag_match_is_returned(self):
        actor = _FakeBindingActor(1, "StaticMeshActor_998", uclass="StaticMeshActor",
                                  mesh_components=1)
        service = _FakeBindingService([actor], tags={"MuJoCoBody=base_link": [actor]})

        self.assertIs(
            _find_actor(_FakeBindingGame(service), actor_tag="MuJoCoBody=base_link"),
            actor,
        )

    def test_ambiguous_tag_is_fatal_and_never_spawns_an_anchor(self):
        first = _FakeBindingActor(1, "a", uclass="StaticMeshActor", mesh_components=1)
        second = _FakeBindingActor(2, "b", uclass="StaticMeshActor", mesh_components=1)
        service = _FakeBindingService(
            [first, second], tags={"MuJoCoBody=link_1": [first, second]})
        game = _FakeBindingGame(service)

        with self.assertRaises(RuntimeError) as context:
            _find_actor(game, actor_tag="MuJoCoBody=link_1")
        self.assertIn("ambiguous actor tag", str(context.exception))

        with self.assertRaises(RuntimeError):
            _find_or_spawn_anchor(game, "link_1", actor_tag="MuJoCoBody=link_1")
        self.assertEqual(service.spawned, [])

    def test_missing_tag_api_degrades_to_the_other_lookups(self):
        game, service, _, _ = _link_hierarchy_game(with_armor=False, tag_api=False)

        with self.assertRaises(_SceneActorNotFound):
            _find_actor(game, actor_tag="MuJoCoBody=base_link")

        anchor = _find_or_spawn_anchor(game, "base_link", actor_tag="MuJoCoBody=base_link")
        self.assertIs(anchor, service.spawned[0])

        # Without any visuals to bind, link_hierarchy mode reports the missing
        # meshes instead of failing on the absent tag API.
        with self.assertRaises(RuntimeError) as context:
            apply_scene_binding_plan(game, _link_hierarchy_plan(links=("base_link",), armor=False))
        self.assertIn("no UStaticMeshComponent", str(context.exception))
