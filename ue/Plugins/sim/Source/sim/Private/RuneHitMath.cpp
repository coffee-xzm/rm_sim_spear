#include "RuneHitMath.h"

FRuneHitCalculation RuneHitMath::Calculate(const FTransform& TargetTransform, const FVector& ImpactPoint, double TargetRadiusCm)
{
	FRuneHitCalculation Result;
	if (TargetRadiusCm <= 0.0 || !FMath::IsFinite(TargetRadiusCm) || ImpactPoint.ContainsNaN())
	{
		return Result;
	}
	const FVector Local = TargetTransform.InverseTransformPosition(ImpactPoint);
	if (Local.ContainsNaN())
	{
		return Result;
	}
	const double RadiusCm = FMath::Sqrt(Local.X * Local.X + Local.Y * Local.Y);
	Result.RadiusMm = FMath::RoundToInt(RadiusCm * 10.0);
	const int32 TargetRadiusMm = FMath::RoundToInt(TargetRadiusCm * 10.0);
	if (Result.RadiusMm > TargetRadiusMm)
	{
		Result.RadiusMm = INDEX_NONE;
		return Result;
	}

	Result.bValid = true;
	const double RingWidthMm = TargetRadiusMm / 10.0;
	Result.Ring = FMath::Clamp(10 - FMath::FloorToInt(Result.RadiusMm / RingWidthMm), 1, 10);
	return Result;
}
