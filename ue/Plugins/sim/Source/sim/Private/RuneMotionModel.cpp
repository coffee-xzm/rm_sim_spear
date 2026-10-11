#include "RuneMotionModel.h"

namespace
{
	constexpr double FixedSpeed = UE_PI / 3.0;
}

void FRuneMotionModel::BeginMatch(int32 Seed)
{
	Random.Initialize(Seed);
	Snapshot = FRuneMotionSnapshot();
	Snapshot.Direction = Random.RandRange(0, 1) == 0 ? -1 : 1;
	LargeActivationTime = 0.0;
	ResetLargeParameters();
}

void FRuneMotionModel::SetMode(ESceneMode NewMode)
{
	Mode = NewMode;
	if (Mode == ESceneMode::LargeRune)
	{
		LargeActivationTime = 0.0;
		ResetLargeParameters();
	}
}

const FRuneMotionSnapshot& FRuneMotionModel::AdvanceTime(double DeltaSeconds, bool bAnySideActivating)
{
	DeltaSeconds = FMath::Max(0.0, DeltaSeconds);
	double DeltaAngle = FixedSpeed * DeltaSeconds;
	Snapshot.SpeedRadPerSecond = FixedSpeed;

	if (Mode == ESceneMode::LargeRune && bAnySideActivating)
	{
		const double Start = LargeActivationTime;
		const double End = Start + DeltaSeconds;
		Snapshot.SpeedRadPerSecond = Snapshot.A * FMath::Sin(Snapshot.Omega * End) + Snapshot.B;
		DeltaAngle = Snapshot.B * DeltaSeconds
			- (Snapshot.A / Snapshot.Omega)
			* (FMath::Cos(Snapshot.Omega * End) - FMath::Cos(Snapshot.Omega * Start));
		LargeActivationTime = End;
	}

	Snapshot.AngleRad += DeltaAngle * Snapshot.Direction;
	return Snapshot;
}

void FRuneMotionModel::ResetLargeParameters()
{
	Snapshot.A = Random.FRandRange(0.780f, 1.045f);
	Snapshot.Omega = Random.FRandRange(1.884f, 2.000f);
	Snapshot.B = 2.090 - Snapshot.A;
}
