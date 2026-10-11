#pragma once

#include "CoreMinimal.h"
#include "RuneSnapshot.h"

class SIM_API FRuneRuleStateMachine
{
public:
	explicit FRuneRuleStateMachine(const FRuneRuleConfig& InConfig = FRuneRuleConfig());

	ERuneTransitionReason Reset(ESceneMode NewMode, int32 Seed);
	ERuneTransitionReason SetMode(ESceneMode NewMode);
	ERuneTransitionReason AdvanceTime(double DeltaSeconds);
	ERuneTransitionReason HandleHit(int32 ArmorIndex, int32 RadiusMm = INDEX_NONE, int32 Ring = 0);

	const FRuneSnapshot& GetSnapshot() const { return Snapshot; }
	double GetCurrentTimeSeconds() const { return CurrentTimeSeconds; }

private:
	ERuneTransitionReason BeginRound();
	ERuneTransitionReason FinishRound();
	ERuneTransitionReason CompleteActivation();
	ERuneTransitionReason Fail(ERuneTransitionReason Reason);
	void SetTransition(ERuneTransitionReason Reason);
	int32 PickIndex(const TArray<int32>& Candidates);

	FRuneRuleConfig Config;
	FRuneSnapshot Snapshot;
	FRandomStream Random;
	double CurrentTimeSeconds = 0.0;
	double CompletionStartedSeconds = 0.0;
};
