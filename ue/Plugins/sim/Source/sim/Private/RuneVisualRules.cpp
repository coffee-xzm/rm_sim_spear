#include "RuneVisualRules.h"

FRuneArmLightVisualState RuneVisualRules::ResolveArmLights(
	const FRuneSnapshot& Snapshot,
	int32 ArmIndex)
{
	FRuneArmLightVisualState Result;
	if (Snapshot.State == ERuneState::Activated)
	{
		const ERuneResolvedLightMaterial CompletionMaterial = Snapshot.bCompletionFlashOn
			? ERuneResolvedLightMaterial::Full
			: ERuneResolvedLightMaterial::Metal;
		Result.Horizontal = CompletionMaterial;
		Result.Vertical = CompletionMaterial;
		Result.Flow = CompletionMaterial;
		return Result;
	}
	if (Snapshot.State != ERuneState::Activating)
	{
		return Result;
	}

	if (Snapshot.Mode == ESceneMode::LargeRune)
	{
		// Large-rune horizontal lights and flow stay illuminated throughout an
		// activation round. The currently selectable fan is distinguished by its
		// arrow flow; vertical lights remain metal until final completion.
		Result.Horizontal = ERuneResolvedLightMaterial::Active;
		Result.Flow = Snapshot.LitArmorIndices.Contains(ArmIndex)
			? ERuneResolvedLightMaterial::Arrow
			: ERuneResolvedLightMaterial::Active;
		return Result;
	}

	if (Snapshot.CompletedArmorIndices.Contains(ArmIndex))
	{
		Result.Horizontal = ERuneResolvedLightMaterial::Active;
		Result.Flow = ERuneResolvedLightMaterial::Active;
		return Result;
	}

	if (Snapshot.LitArmorIndices.Contains(ArmIndex))
	{
		Result.Flow = ERuneResolvedLightMaterial::Arrow;
	}
	return Result;
}

ERuneResolvedFanMaterial RuneVisualRules::ResolveFan(
	const FRuneSnapshot& Snapshot,
	int32 ArmIndex)
{
	const int32 HitRing = Snapshot.ArmorHitRings.IsValidIndex(ArmIndex)
		? Snapshot.ArmorHitRings[ArmIndex]
		: 0;
	if (Snapshot.State == ERuneState::Activated)
	{
		return HitRing > 0 ? ERuneResolvedFanMaterial::Ring : ERuneResolvedFanMaterial::Active;
	}
	if (Snapshot.State != ERuneState::Activating)
	{
		return ERuneResolvedFanMaterial::Metal;
	}
	if (Snapshot.Mode == ESceneMode::LargeRune)
	{
		return Snapshot.LitArmorIndices.Contains(ArmIndex)
			? ERuneResolvedFanMaterial::Active
			: ERuneResolvedFanMaterial::Metal;
	}
	if (HitRing > 0)
	{
		return ERuneResolvedFanMaterial::Ring;
	}
	return Snapshot.LitArmorIndices.Contains(ArmIndex)
		|| Snapshot.CompletedArmorIndices.Contains(ArmIndex)
		? ERuneResolvedFanMaterial::Active
		: ERuneResolvedFanMaterial::Metal;
}
