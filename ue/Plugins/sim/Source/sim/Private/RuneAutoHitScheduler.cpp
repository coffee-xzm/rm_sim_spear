#include "RuneAutoHitScheduler.h"

int32 FRuneAutoHitScheduler::Advance(
	const FRuneSnapshot& Snapshot,
	double DeltaSeconds,
	bool bEnabled,
	double DelaySeconds)
{
	if (!bEnabled || Snapshot.State != ERuneState::Activating
		|| Snapshot.Phase != ERuneRoundPhase::AwaitFirstHit
		|| Snapshot.LitArmorIndices.IsEmpty())
	{
		Reset();
		return INDEX_NONE;
	}

	const int32 Candidate = Snapshot.LitArmorIndices[0];
	if (Candidate != TrackedArmorIndex)
	{
		TrackedArmorIndex = Candidate;
		LitSeconds = 0.0;
	}

	LitSeconds += FMath::Max(DeltaSeconds, 0.0);
	if (LitSeconds + UE_SMALL_NUMBER < FMath::Max(DelaySeconds, 0.0))
	{
		return INDEX_NONE;
	}

	Reset();
	return Candidate;
}

void FRuneAutoHitScheduler::Reset()
{
	TrackedArmorIndex = INDEX_NONE;
	LitSeconds = 0.0;
}
