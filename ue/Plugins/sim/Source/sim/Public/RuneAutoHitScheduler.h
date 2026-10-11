#pragma once

#include "CoreMinimal.h"
#include "RuneSnapshot.h"

class SIM_API FRuneAutoHitScheduler
{
public:
	int32 Advance(const FRuneSnapshot& Snapshot, double DeltaSeconds, bool bEnabled, double DelaySeconds);
	void Reset();

private:
	int32 TrackedArmorIndex = INDEX_NONE;
	double LitSeconds = 0.0;
};
