#pragma once

#include "CoreMinimal.h"
#include "RuneTypes.h"

struct SIM_API FRuneMotionSnapshot
{
	double SpeedRadPerSecond = 0.0;
	double AngleRad = 0.0;
	double A = 0.0;
	double Omega = 0.0;
	double B = 0.0;
	int32 Direction = 1;
};

class SIM_API FRuneMotionModel
{
public:
	void BeginMatch(int32 Seed);
	void SetMode(ESceneMode NewMode);
	const FRuneMotionSnapshot& AdvanceTime(double DeltaSeconds, bool bAnySideActivating);
	const FRuneMotionSnapshot& GetSnapshot() const { return Snapshot; }

private:
	void ResetLargeParameters();

	FRandomStream Random;
	ESceneMode Mode = ESceneMode::Inactive;
	double LargeActivationTime = 0.0;
	FRuneMotionSnapshot Snapshot;
};
