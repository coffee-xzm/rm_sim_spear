from dataclasses import dataclass
import struct
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree

import mujoco
import numpy as np

from .model import Pose


#: Bodies that get a shell box alongside the wheel cylinders.  Keep this short
#: and measured, not derived -- see ``PhysicsOptions.shell_bodies``.
#:
#: Measured 2026-10-10 on ``总装`` with the envelope export:
#:   ``()``                      -> 5 colliders, 73.597 mm,  8 contacts, 32 546 steps/s
#:   ``("base_link",)``          -> 6 colliders, 73.597 mm,  8 contacts, 26 972 steps/s
#:   ``("base_link","link_5","link_7")`` -> 8 colliders, 116.689 mm (rests on the gimbal)
#:
#: ``base_link`` alone costs ~17 % step rate and buys "the chassis cannot drive
#: through a wall" without disturbing the rest pose at all.
DEFAULT_SHELL_BODIES: tuple[str, ...] = ("base_link",)

#: Display group for every geometry that exists only to collide.
#:
#: MuJoCo draws geom groups 0/1/2 by default (``mjvOption.geomgroup`` is
#: ``[1,1,1,0,0,0]``) and hides 3/4/5.  The URDF importer puts ``<collision>``
#: in group 0 and ``<visual>`` in group 1 -- measured on ``总装``: 955 mesh
#: geoms in each -- so *both* are painted, and the exported CAD collision shell
#: buries the mechanism: the joints, the wheels and the gimbal are all behind
#: 955 coincident surfaces.  Group 3 is the conventional collision-only group,
#: so the default picture is the robot and one tick in the viewer's Rendering
#: panel (Geom Group 3) reveals the colliders.  The floor stays in group 0: it
#: is the ground reference, not a collision hull.
COLLISION_GEOM_GROUP = 3

#: Prefix for every geom the arena adds.  ``_apply_simple_collision`` mutes
#: everything it did not build, so the arena geoms have to be recognisable by
#: name; a shared prefix is cheaper and harder to get wrong than threading a
#: set of names through the options.
ARENA_GEOM_PREFIX = "arena_"

#: The arena's height field.  Like the plain ``floor`` plane it is the ground
#: *reference*, so it stays in the visible group: hiding it would leave the
#: viewer with a robot standing on nothing.
ARENA_GROUND_GEOM = "arena_ground"


@dataclass(frozen=True)
class PhysicsOptions:
    """How the generated URDF should be turned into a *simulated* model.

    The URDF contract describes the robot; it does not describe a world.  The
    SolidWorks export therefore compiles to a model whose ``base_link`` is
    welded to ``world`` (no joint), with no actuators and no ground geometry:
    ``nq=6`` is just the four wheel hinges plus the gimbal yaw/pitch, so nothing
    can ever move the chassis.  These options add the missing world through
    MuJoCo's ``MjSpec`` API instead of hand-editing the generated XML.
    """

    free_base: bool = False
    """Give the root link a free joint so gravity, contacts and drive act on it."""

    driven_joints: tuple[str, ...] = ()
    """Joint names to expose as velocity actuators (``nu`` becomes this length)."""

    floor: bool = False
    """Add a ground plane to the world body; without one a free base falls forever."""

    root_link: str = "base_link"
    drive_gain: float = 1.0
    """Velocity-servo gain.  10.0 (with armature 0.001) blows up: measured
    ``QACC`` overflow at t=0.005 s, after which the chassis collapses onto the
    floor and the trace stops making sense.  1.0 with armature 0.01 is stable.
    """
    max_joint_speed: float = 20.0
    joint_armature: float = 0.01
    joint_damping: float = 0.0
    """Damping applied to every movable joint.

    The SolidWorks export has no damping at all; with a free base the gimbal
    chain (``link_5``/``link_7``) whips and the integration diverges
    (measured: ``|qvel| = 1394`` after half a second).  2.0 settles the chain.
    """

    simple_collision: bool = False
    """Replace the CAD collision geometry with cylinders/boxes.

    The export carries 1904 mesh geoms.  Colliding those with each other and the
    floor is both expensive and unstable; the wheels are rebuilt as cylinders
    sized from the mesh bounds and everything else stops colliding.
    """

    shell_bodies: tuple[str, ...] = ()
    """Bodies that additionally get **one** box collider, their mesh AABB.

    Only meaningful together with :attr:`simple_collision`.  Without this the
    robot's *only* colliders are the four wheel cylinders, so the chassis passes
    through anything above wheel height; this adds the "shell".

    Keep the list short and chosen, not derived: a body's AABB is the swept
    volume of everything in it, so picking the wrong body puts the collider below
    the wheels.  Measured 2026-10-10 on ``总装`` -- ``link_5``'s AABB is
    203x196x225 mm and, with it included, the robot rests on the gimbal at
    ``base_link z = 187.83 mm`` instead of standing on its wheels at 73.39 mm.
    ``base_link`` alone is safe: its CAD bottom is 46.65 mm while the wheels
    reach 56.44 mm, so it cannot take the load.
    """

    chassis_box: tuple[float, float, float] | None = None
    chassis_pos: tuple[float, float, float] = (0.0, 0.0, 0.10)
    floor_size: tuple[float, float, float] = (5.0, 5.0, 0.1)
    floor_friction: tuple[float, float, float] = (1.0, 0.005, 0.0001)
    wheel_friction: tuple[float, float, float] = (1.2, 0.01, 0.0001)
    timestep: float | None = None

    arena_scene: Path | None = None
    """A whole MJCF world exported by the Blender ``blender_mujoco_terrain`` addon.

    This is the only arena the project has.  It was previously one of two, and
    the two disagreed about the sign of y -- see ``arena_scene_mirror_y`` below
    and ``docs/rmuc26_asset_review.md``.  The other one (``arena.json``, built by
    the now-deleted ``pipeline/arena_scene.py``) was the odd one out: the robot
    and this export are both the level reflected in y, and only ``arena.json``
    was not.  Keeping one source removes that whole class of ambiguity.

    Two properties of the export are not obvious and both bite silently:

    ``arena_scene_mirror_y``
        The addon works in Blender's frame, which is right-handed; Unreal's is
        left-handed.  The export is therefore the level **reflected** in y
        relative to Unreal: every one of the 8 shared objects has its y negated
        (``blender_for_ue5_node_22`` is at y = -1.650 in the file and +1.650 in
        the level), and the field is not symmetric, so an unmirrored import
        would be a different arena with the ramps on the wrong side.

        **But the pose bridge already applies exactly that reflection.**
        :func:`rm_sim_spear.pose_sync.to_unreal_pose` converts with
        ``100 * diag(1, -1, 1)``, so a MuJoCo point maps to Unreal with its y
        negated.  The file is therefore *already* in the frame the bridge wants,
        and mirroring it on import negates y a second time.  Measured against
        Unreal's own ``actor.get_actor_bounds()`` through the bridge: importing
        as exported puts 8/9 objects within 0.03 cm of their actor bounds with
        the offset below; importing mirrored scatters 6 of them across 9 m.

        That is why the default is ``False`` -- the flag is only for an export
        made by a tool that did negate y itself.  Run
        ``tools/check_arena_scene.py`` on any new export rather than assuming:
        it reports which setting that particular file needs.

    ``arena_scene_drop_geoms``
        The addon approximates every object that was added as a plain "extra
        collider" by a **single convex hull**; only regions cut out of the
        height field may use CoACD or thin prisms.  On this level one object
        (``SM_MERGED_Actor_424``, 1.57M source faces) hulls to 750 m3 over a
        26.6 x 13.9 m footprint and swallows the whole field: with it present the
        robot starts 0.81 m inside it at the field centre (3679 contacts) and
        settles 0.53 m *below* the ground, while deleting that one geom alone
        leaves it resting on the ground.  A robot spawned higher does not help --
        it lands *on* the hull, ~2 m above the floor, because the hull is solid.
    """

    arena_scene_mirror_y: bool = False
    """Negate y while importing :attr:`arena_scene`; see that field."""

    arena_scene_drop_geoms: tuple[str, ...] = ()
    """Geom names to delete from :attr:`arena_scene`; see that field."""

    root_pos_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    """Where the root body starts, in the model frame.

    The generated URDF places ``base_link`` at the origin and the model used to
    compensate with ``--world-offset-cm``.  With a real arena there is nothing
    to compensate: the robot sits where the level puts it, which is what
    ``arena.json``'s ``spawn.ground_z_m`` reports.
    """

    def _arena_scene_base_spec(self) -> "mujoco.MjSpec":
        """Turn the addon's scene export into the world the robot is attached to.

        The addon writes ``<include file="robot.xml"/>`` as the seam where the
        robot goes.  We supply our own robot instead, but the include has to
        resolve or the whole file fails to load -- so every include the file
        mentions is satisfied with an empty model.  It is read out of the file
        rather than hard-coded to ``robot.xml`` because that name is the addon's
        convention, not a property of the format.

        Mirroring is done by negating y on every *placement* and on every mesh's
        scale, which is a reflection and therefore changes the handedness of the
        mesh triangles.  MuJoCo refuses a signed volume, so the mirrored meshes
        fall back to ``mjMESH_INERTIA_LEGACY``; every geom here is static
        (``density="0"``), so the inertia value is never used for anything.
        Rotations have to be conjugated (``M R M``), not just copied -- a body
        that is rotated 90 degrees about z would otherwise be mirrored into the
        wrong quadrant.
        """
        assert self.arena_scene is not None
        path = Path(self.arena_scene)
        if not path.is_file():
            raise ValueError(f"arena scene not found: {path}")
        text = path.read_text(encoding="utf-8")
        includes = {
            element.attrib["file"]: b"<mujoco><worldbody/></mujoco>"
            for element in ElementTree.fromstring(text).iter("include")
            if "file" in element.attrib
        }
        spec = mujoco.MjSpec.from_file(str(path), include=includes or None)

        # The addon always writes a bare "floor" plane at z = 0 for its own
        # preview.  It is *not* the level's ground -- measured on assets/rmuc26
        # it sits 1.45 m above the field -- so a robot left standing on it is
        # standing on nothing.  It has to go, and it has to go explicitly:
        # satisfying the include happens to drop it today, but that is a
        # side-effect of how MuJoCo merges two <worldbody> elements, not a
        # promise.  A top-level geom lives in ``worldbody.geoms`` and not in
        # ``spec.geoms``, so neither the naming pass nor ``_apply_simple_collision``
        # would ever see it.
        for geom in list(spec.worldbody.geoms):
            if geom.type == mujoco.mjtGeom.mjGEOM_PLANE:
                spec.delete(geom)

        for name in self.arena_scene_drop_geoms:
            geom = spec.geom(name)
            if geom is None:
                raise ValueError(f"arena scene has no geom named {name!r} to drop")
            spec.delete(geom)

        if self.arena_scene_mirror_y:
            flip = np.diag([1.0, -1.0, 1.0])
            for mesh in spec.meshes:
                scale = list(mesh.scale)
                mesh.scale = [scale[0], -scale[1], scale[2]]
                mesh.inertia = mujoco.mjtMeshInertia.mjMESH_INERTIA_LEGACY
            for hfield in spec.hfields:
                self._mirror_hfield(hfield)
            for body in spec.bodies:
                self._mirror_y(body, flip)
            for geom in spec.geoms:
                self._mirror_y(geom, flip)
            for light in spec.lights:
                self._mirror_y(light, flip)

        for index, hfield in enumerate(spec.hfields):
            hfield.name = (ARENA_GROUND_GEOM if index == 0
                           else f"{ARENA_GEOM_PREFIX}hfield{index}")
        for index, geom in enumerate(spec.geoms):
            # Every geom the arena contributes is static.  Saying so explicitly
            # matters once y is mirrored: a negative extent makes the derived
            # volume -- and therefore the derived mass -- negative, which MuJoCo
            # rejects outright.
            geom.density = 0.0
            if geom.type == mujoco.mjtGeom.mjGEOM_HFIELD:
                # The addon parks its height field in the collision group; it is
                # the ground reference here, so it belongs in the visible one.
                geom.name = geom.hfieldname = ARENA_GROUND_GEOM
                geom.group = 0
            else:
                geom.name = f"{ARENA_GEOM_PREFIX}{geom.name or f'geom{index}'}"
        return spec

    def _mirror_hfield(self, hfield) -> None:
        """Point one height field at a row-reversed copy of its data file.

        A height field is a grid of rows running along y, so reflecting it means
        reversing the rows.  Negating the extent instead would express the same
        reflection, and is what this first tried, but MuJoCo rejects a
        non-positive ``size`` outright ("size parameter is not positive in
        hfield"), so the samples have to be written out in the other order.

        The copy lands next to the original -- same directory, ``_mirror_y``
        appended -- because the file is referenced relatively from the scene XML
        and nothing else in the model knows where "next to it" is.  It is
        rewritten only when the source is newer, so the cost is one pass.
        """
        assert self.arena_scene is not None
        source = Path(hfield.file)
        if not source.is_absolute():
            source = Path(self.arena_scene).parent / source
        source = source.resolve()
        target = source.with_name(f"{source.stem}_mirror_y{source.suffix}")
        if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
            raw = source.read_bytes()
            nrow, ncol = struct.unpack("<ii", raw[:8])
            data = np.frombuffer(raw[8:8 + nrow * ncol * 4], "<f4").reshape(nrow, ncol)
            target.write_bytes(struct.pack("<ii", nrow, ncol)
                               + np.ascontiguousarray(data[::-1]).tobytes())
        hfield.file = str(target)

    @staticmethod
    def _mirror_y(item, flip: np.ndarray) -> None:
        """Reflect one spec element's placement through the y = 0 plane."""
        pos = list(item.pos)
        item.pos = [pos[0], -pos[1], pos[2]]
        direction = getattr(item, "dir", None)
        if direction is not None:
            item.dir = [direction[0], -direction[1], direction[2]]
        quat = getattr(item, "quat", None)
        if quat is None:
            return
        quat = np.asarray(quat, dtype=float)
        if not np.allclose(quat, [1.0, 0.0, 0.0, 0.0]):
            matrix = np.zeros(9)
            mujoco.mju_quat2Mat(matrix, quat)
            mirrored = flip @ matrix.reshape(3, 3) @ flip
            out = np.zeros(4)
            mujoco.mju_mat2Quat(out, np.ascontiguousarray(mirrored.ravel()))
            item.quat = list(out)

    def build_spec(self, path: str | Path) -> "mujoco.MjSpec":
        if self.arena_scene is not None:
            # The arena *is* the world here, so the robot is attached into it
            # rather than the other way round.  The attach prefix is the child
            # spec's separator, so the root comes back as "/base_link"; strip it
            # because every other option names links unqualified.
            spec = self._arena_scene_base_spec()
            spec.attach(mujoco.MjSpec.from_file(str(path)),
                        frame=spec.worldbody.add_frame())
            for element in (*spec.bodies, *spec.joints, *spec.sites):
                if element.name.startswith("/"):
                    element.name = element.name[1:]
        else:
            spec = mujoco.MjSpec.from_file(str(path))
            if self.floor:
                geom = spec.worldbody.add_geom()
                geom.name = "floor"
                geom.type = mujoco.mjtGeom.mjGEOM_PLANE
                geom.size = list(self.floor_size)
                geom.condim = 3
                geom.friction = list(self.floor_friction)
        if self.free_base:
            body = spec.body(self.root_link)
            if body is None:
                raise ValueError(f"root link not found in model: {self.root_link!r}")
            # The generated URDF puts the root at the origin.  Setting it before
            # add_freejoint() is what makes that the free joint's qpos0, which is
            # how the robot is placed on a real arena instead of on a plane that
            # was assumed to pass through z = 0.
            if tuple(self.root_pos_m) != (0.0, 0.0, 0.0):
                body.pos = list(self.root_pos_m)
            body.add_freejoint()
        for name in self.driven_joints:
            if spec.joint(name) is None:
                raise ValueError(f"driven joint not found in model: {name!r}")
            actuator = spec.add_actuator()
            actuator.name = f"drive_{name}"
            # The transmission type must be set before the target, otherwise
            # MjSpec rejects the actuator with "invalid transmission type".
            actuator.trntype = mujoco.mjtTrn.mjTRN_JOINT
            actuator.target = name
            actuator.set_to_velocity(self.drive_gain)
            actuator.ctrllimited = True
            actuator.ctrlrange = [-self.max_joint_speed, self.max_joint_speed]
        if self.joint_armature or self.joint_damping:
            for name in self._movable_joints(spec):
                joint = spec.joint(name)
                if self.joint_armature:
                    joint.armature = self.joint_armature
                if self.joint_damping:
                    joint.damping = [self.joint_damping] * 3
        # Before simple_collision(), which mutes the export's <collision> set:
        # afterwards there is no longer any way to tell a collider from a
        # visual, and the muted mesh would keep burying the robot.
        self.hide_collision_geometry(spec)
        if self.simple_collision:
            self._apply_simple_collision(spec)
        if self.timestep is not None:
            spec.option.timestep = self.timestep
        return spec

    @staticmethod
    def hide_collision_geometry(spec: "mujoco.MjSpec") -> None:
        """Move the export's own colliders out of the default picture.

        Identification is by ``contype``/``conaffinity``, so this has to run
        while the URDF's ``<collision>`` geoms are still live -- i.e. before
        :meth:`_apply_simple_collision` mutes them.  The floor is exempt: it is
        the ground reference the operator needs to see, not a collision hull.

        Group visibility is a *drawing* concern only; no contact changes.
        """
        for geom in spec.geoms:
            if geom.name in ("floor", ARENA_GROUND_GEOM):
                continue
            if geom.contype or geom.conaffinity:
                geom.group = COLLISION_GEOM_GROUP

    @staticmethod
    def _movable_joints(spec: "mujoco.MjSpec") -> tuple[str, ...]:
        """Names of the hinge/slide joints (free joints have no name to damp)."""
        names = []
        for joint in spec.joints:
            if joint.name and joint.name != "world":
                names.append(joint.name)
        return tuple(names)

    def _apply_simple_collision(self, spec: "mujoco.MjSpec") -> None:
        """Keep only the generated colliders: floor, wheel cylinders, shell boxes.

        Every collider this keeps is *generated* here, never taken from the
        export's own ``<collision>``.  The export's geometry is unusable as a
        collider set: paired with the visual it is 955 overlapping CAD parts
        (measured: 109 contacts, robot resting on a chassis corner), and paired
        with the exporter's boxes it is 8 grossly oversized boxes in the wheel
        links (measured: the robot 50 mm off the ground, 709 contacts).
        """
        if not self.driven_joints:
            raise ValueError("simple_collision requires driven_joints to build wheels from")
        model = spec.compile()
        # The arena's own geoms collide on their own terms and are not part of
        # the export's CAD collision set; muting them here silently removed the
        # ground (measured: the robot fell 29 m with ncon = 0).
        keep = {"floor"}
        keep.update(geom.name for geom in spec.geoms
                    if geom.name and geom.name.startswith(ARENA_GEOM_PREFIX))
        for name in self.driven_joints:
            joint = model.joint(name)
            body_id = int(joint.bodyid[0])
            body_name = str(model.body(body_id).name)
            if not any(int(model.geom_bodyid[index]) == body_id
                       and int(model.geom_type[index]) == int(mujoco.mjtGeom.mjGEOM_MESH)
                       for index in range(model.ngeom)):
                continue
            radius, half_length, centre = wheel_cylinder(model, name)
            geom = spec.body(body_name).add_geom()
            geom.name = f"wheel_{name}"
            geom.type = mujoco.mjtGeom.mjGEOM_CYLINDER
            # A cylinder's axis is +Z; the wheel's axle is the joint axis.
            geom.size = [radius, half_length, 0.0]
            geom.pos = list(np.asarray(joint.pos, dtype=float)
                            + np.asarray(joint.axis, dtype=float) * centre)
            geom.quat = list(_quat_from_z_to(list(joint.axis)))
            geom.condim = 3
            geom.friction = list(self.wheel_friction)
            geom.group = COLLISION_GEOM_GROUP
            keep.add(geom.name)
        for body_name in self.shell_bodies:
            body_id = model.body(body_name).id
            vertices = list(_mesh_vertices_in_body_frame(model, body_id))
            if not vertices:
                raise ValueError(
                    f"shell body has no mesh geometry to size a box from: {body_name!r}")
            points = np.vstack(vertices)
            lower, upper = points.min(axis=0), points.max(axis=0)
            geom = spec.body(body_name).add_geom()
            geom.name = f"shell_{body_name}"
            geom.type = mujoco.mjtGeom.mjGEOM_BOX
            geom.size = list((upper - lower) / 2.0)
            geom.pos = list((upper + lower) / 2.0)
            geom.condim = 3
            geom.friction = list(self.wheel_friction)
            geom.group = COLLISION_GEOM_GROUP
            keep.add(geom.name)
        if self.chassis_box is not None:
            chassis = spec.body(self.root_link).add_geom()
            chassis.name = "chassis"
            chassis.type = mujoco.mjtGeom.mjGEOM_BOX
            chassis.size = list(self.chassis_box)
            chassis.pos = list(self.chassis_pos)
            chassis.condim = 3
            chassis.group = COLLISION_GEOM_GROUP
            keep.add(chassis.name)
        for geom in spec.geoms:
            if geom.name not in keep:
                geom.contype = 0
                geom.conaffinity = 0


def _mesh_vertices_in_body_frame(model: "mujoco.MjModel", body_id: int):
    """Yield each mesh geom's vertices of ``body_id``, in that body's frame."""
    for index in range(model.ngeom):
        if int(model.geom_bodyid[index]) != body_id:
            continue
        if int(model.geom_type[index]) != int(mujoco.mjtGeom.mjGEOM_MESH):
            continue
        geom = model.geom(index)
        matrix = np.zeros(9)
        mujoco.mju_quat2Mat(matrix, np.asarray(geom.quat, dtype=float))
        data_id = int(model.geom_dataid[index])
        start = int(model.mesh_vertadr[data_id])
        stop = start + int(model.mesh_vertnum[data_id])
        yield (np.asarray(model.mesh_vert[start:stop], dtype=float)
               @ matrix.reshape(3, 3).T + np.asarray(geom.pos, dtype=float))


def wheel_cylinder(model: "mujoco.MjModel", joint_name: str, *,
                   rim_fraction: float = 0.95) -> tuple[float, float, float]:
    """Cylinder that stands in for the wheel driven by ``joint_name``.

    Returns ``(radius, half_length, centre)`` in the wheel body's frame, where
    ``centre`` is measured along the joint axis from the joint anchor.

    The rim has to come from the mesh vertices, not from ``geom_aabb``: MuJoCo
    reports each geom's AABB **in that geom's own frame**, so unioning those
    drops every part's ``pos``/``quat`` and reduces to "the largest single part".
    For these omni wheels that is the 55.5 mm side plate instead of the 60.1 mm
    tyre, which leaves the CAD 4.6 mm inside the floor once the plane is where
    the level's floor is.  Vertices within ``rim_fraction`` of the largest
    radius set the axial extent, so the cylinder is as wide as the rolling
    surface and no wider.
    """
    joint = model.joint(joint_name)
    body_id = int(joint.bodyid[0])
    axis = np.asarray(joint.axis, dtype=float)
    axis = axis / np.linalg.norm(axis)
    anchor = np.asarray(joint.pos, dtype=float)

    def axial_and_radial():
        for vertices in _mesh_vertices_in_body_frame(model, body_id):
            relative = vertices - anchor
            axial = relative @ axis
            radial = np.linalg.norm(relative - axial[:, None] * axis, axis=1)
            yield axial, radial

    radius = 0.0
    for _, radial in axial_and_radial():
        radius = max(radius, float(radial.max()))
    limit = radius * float(rim_fraction)
    low, high = np.inf, -np.inf
    for axial, radial in axial_and_radial():
        rim = axial[radial >= limit]
        if rim.size:
            low = min(low, float(rim.min()))
            high = max(high, float(rim.max()))
    if not np.isfinite(low):
        low = high = 0.0
    return radius, max((high - low) / 2.0, 1e-3), (low + high) / 2.0


def _quat_wxyz(unreal_quat_xyzw: Sequence[float]) -> list[float]:
    """Reorder Unreal's ``(x, y, z, w)`` quaternion into MuJoCo's ``(w, x, y, z)``.

    The two conventions hold the same four numbers in a different sequence, and
    both are unit quaternions, so handing one to a caller that wants the other is
    *silent*: ``MjSpec`` accepts it and the geom still lands somewhere plausible.
    Measured on this arena: 411 of the 463 placements were rotated by a median of
    120 degrees, up to 180, and the field rendered as fans of oversized slabs
    radiating from the centre.
    """
    x, y, z, w = (float(value) for value in unreal_quat_xyzw)
    return [w, x, y, z]


def _quat_from_z_to(axis: Sequence[float]) -> list[float]:
    """Rotation (w, x, y, z) that maps +Z onto ``axis``."""
    norm = sum(component * component for component in axis) ** 0.5
    if norm == 0.0:
        return [1.0, 0.0, 0.0, 0.0]
    unit = [component / norm for component in axis]
    dot = unit[2]
    if dot > 1.0 - 1e-12:
        return [1.0, 0.0, 0.0, 0.0]
    if dot < -1.0 + 1e-12:
        return [0.0, 1.0, 0.0, 0.0]
    cross = [-unit[1], unit[0], 0.0]
    scale = (2.0 * (1.0 + dot)) ** 0.5
    return [scale / 2.0, cross[0] / scale, cross[1] / scale, cross[2] / scale]


class MujocoDriver:
    def __init__(self, model: mujoco.MjModel, steps_per_frame: int):
        if steps_per_frame < 1:
            raise ValueError("steps_per_frame must be positive")
        self.model = model
        self.data = mujoco.MjData(model)
        self.steps_per_frame = steps_per_frame
        self.physics_step = 0
        self._viewer = None
        mujoco.mj_forward(self.model, self.data)

    @classmethod
    def from_xml_path(
        cls,
        path: str | Path,
        steps_per_frame: int,
        *,
        physics: PhysicsOptions | None = None,
    ) -> "MujocoDriver":
        """Build a driver, optionally adding a world the URDF cannot express."""
        if physics is None:
            return cls(mujoco.MjModel.from_xml_path(str(path)), steps_per_frame)
        return cls(physics.build_spec(path).compile(), steps_per_frame)

    @property
    def sim_time_ns(self) -> int:
        return round(self.data.time * 1_000_000_000)

    def reset(self) -> None:
        mujoco.mj_resetData(self.model, self.data)
        mujoco.mj_forward(self.model, self.data)
        self.physics_step = 0

    def step(self, control: Sequence[float] = ()) -> None:
        if len(control) != self.model.nu:
            raise ValueError(f"expected {self.model.nu} actuator controls, got {len(control)}")
        if self.model.nu:
            self.data.ctrl[:] = control
        for _ in range(self.steps_per_frame):
            mujoco.mj_step(self.model, self.data)
        self.physics_step += self.steps_per_frame

    def read_poses(self) -> dict[str, Pose]:
        return {
            self.model.body(body_id).name: Pose(
                position_m=tuple(float(x) for x in self.data.xpos[body_id]),
                quaternion_wxyz=tuple(float(x) for x in self.data.xquat[body_id]),
            )
            for body_id in range(1, self.model.nbody)
        }

    def read_sensors(self) -> dict[str, tuple[float, ...]]:
        return {
            self.model.sensor(sensor_id).name: tuple(
                float(x) for x in self.data.sensordata[
                    self.model.sensor_adr[sensor_id]:
                    self.model.sensor_adr[sensor_id] + self.model.sensor_dim[sensor_id]
                ]
            )
            for sensor_id in range(self.model.nsensor)
        }

    #
    # Optional interactive viewer.
    #
    # Mirrors the pattern of SPEAR's own MuJoCo interop example
    # (``examples/mujoco_interop/run.py``): ``mujoco.viewer.launch_passive`` owns
    # an interactive window whose perturbations (double-click a body, drag) are
    # written straight into ``mj_data``, and the simulation loop calls
    # ``sync()`` once per iteration.  Because this project's pose pipeline is a
    # one-way MuJoCo -> UE mirror (``step()`` -> ``read_poses()`` -> link
    # actors), a drag shows up in Unreal on the next transaction with no extra
    # machinery.
    #
    # ``mujoco.viewer`` is imported lazily so headless runs (and the offline test
    # suite) never need a display or the GUI dependencies.
    #

    def open_viewer(self, *, show_right_ui: bool = False) -> None:
        """Open the passive viewer window; no-op if one is already open."""
        if self._viewer is not None:
            return
        import mujoco.viewer  # noqa: PLC0415 - GUI import must stay lazy

        self._viewer = mujoco.viewer.launch_passive(
            self.model, self.data, show_right_ui=show_right_ui)

    def viewer_running(self) -> bool:
        """False once the user closes the viewer window (or if none is open)."""
        return self._viewer is not None and self._viewer.is_running()

    def sync_viewer(self) -> bool:
        """Push the current ``mj_data`` into the window; False when it closed."""
        if self._viewer is None:
            return False
        if not self._viewer.is_running():
            return False
        self._viewer.sync()
        return True

    def close_viewer(self) -> None:
        if self._viewer is not None:
            self._viewer.close()
            self._viewer = None
