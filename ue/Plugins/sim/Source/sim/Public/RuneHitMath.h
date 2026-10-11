#pragma once

#include "CoreMinimal.h"

struct SIM_API FRuneHitCalculation
{
	bool bValid = false;
	int32 RadiusMm = INDEX_NONE;
	int32 Ring = 0;
};

namespace RuneHitMath
{
	SIM_API FRuneHitCalculation Calculate(const FTransform& TargetTransform, const FVector& ImpactPoint, double TargetRadiusCm = 5.0);
}
