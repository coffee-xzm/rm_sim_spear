#pragma once

#include "CoreMinimal.h"
#include "RuneSnapshot.h"

enum class ERuneResolvedLightMaterial : uint8
{
	Metal,
	Active,
	Arrow,
	Full,
};

enum class ERuneResolvedFanMaterial : uint8
{
	Metal,
	Active,
	Ring,
};

struct SIM_API FRuneArmLightVisualState
{
	ERuneResolvedLightMaterial Horizontal = ERuneResolvedLightMaterial::Metal;
	ERuneResolvedLightMaterial Vertical = ERuneResolvedLightMaterial::Metal;
	ERuneResolvedLightMaterial Flow = ERuneResolvedLightMaterial::Metal;
};

namespace RuneVisualRules
{
	SIM_API FRuneArmLightVisualState ResolveArmLights(const FRuneSnapshot& Snapshot, int32 ArmIndex);
	SIM_API ERuneResolvedFanMaterial ResolveFan(const FRuneSnapshot& Snapshot, int32 ArmIndex);
}
