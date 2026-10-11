#include "RuneRuleStateMachine.h"

FRuneRuleStateMachine::FRuneRuleStateMachine(const FRuneRuleConfig& InConfig)
	: Config(InConfig)
{
}

ERuneTransitionReason FRuneRuleStateMachine::Reset(ESceneMode NewMode, int32 Seed)
{
	Random.Initialize(Seed);
	return SetMode(NewMode);
}

ERuneTransitionReason FRuneRuleStateMachine::SetMode(ESceneMode NewMode)
{
	CurrentTimeSeconds = 0.0;
	CompletionStartedSeconds = 0.0;
	Snapshot = FRuneSnapshot();
	Snapshot.Mode = NewMode;
	Snapshot.ArmorHitRings.Init(0, Config.RequiredGroups);

	if (NewMode == ESceneMode::Inactive)
	{
		Snapshot.State = ERuneState::Inactive;
		SetTransition(ERuneTransitionReason::ModeChanged);
		return Snapshot.LastTransition;
	}

	Snapshot.State = ERuneState::Activating;
	BeginRound();
	SetTransition(ERuneTransitionReason::ModeChanged);
	return Snapshot.LastTransition;
}

ERuneTransitionReason FRuneRuleStateMachine::AdvanceTime(double DeltaSeconds)
{
	if (DeltaSeconds < 0.0)
	{
		SetTransition(ERuneTransitionReason::InvalidHit);
		return Snapshot.LastTransition;
	}

	CurrentTimeSeconds += DeltaSeconds;
	if (Snapshot.State == ERuneState::Activated)
	{
		if (CurrentTimeSeconds >= Snapshot.DeadlineSeconds)
		{
			Snapshot.State = ERuneState::Inactive;
			Snapshot.LitArmorIndices.Reset();
			Snapshot.CompletedArmorIndices.Reset();
			Snapshot.ArmorHitRings.Init(0, Config.RequiredGroups);
			Snapshot.ActivatedGroups = 0;
			Snapshot.bCompletionFlashOn = false;
			Snapshot.DeadlineSeconds = 0.0;
			SetTransition(ERuneTransitionReason::ActivationDisplayCompleted);
			return Snapshot.LastTransition;
		}
		const double FlashInterval = FMath::Max(Config.CompletionFlashIntervalSeconds, UE_SMALL_NUMBER);
		const int32 FlashIndex = FMath::FloorToInt(
			(CurrentTimeSeconds - CompletionStartedSeconds) / FlashInterval);
		Snapshot.bCompletionFlashOn = FlashIndex % 2 == 0;
		SetTransition(ERuneTransitionReason::None);
		return Snapshot.LastTransition;
	}
	if (Snapshot.DeadlineSeconds <= 0.0 || CurrentTimeSeconds < Snapshot.DeadlineSeconds)
	{
		SetTransition(ERuneTransitionReason::None);
		return Snapshot.LastTransition;
	}

	if (Snapshot.State == ERuneState::ActivationFailed)
	{
		Snapshot.ActivatedGroups = 0;
		Snapshot.CompletedArmorIndices.Reset();
		Snapshot.ArmorHitRings.Init(0, Config.RequiredGroups);
		Snapshot.State = ERuneState::Activating;
		BeginRound();
		SetTransition(ERuneTransitionReason::FailureRestarted);
		return Snapshot.LastTransition;
	}

	if (Snapshot.State != ERuneState::Activating)
	{
		SetTransition(ERuneTransitionReason::None);
		return Snapshot.LastTransition;
	}

	if (Snapshot.Phase == ERuneRoundPhase::AwaitSecondHit)
	{
		// The second target remains visible for the full window, even when it was hit.
		return FinishRound();
	}

	return Fail(ERuneTransitionReason::FirstHitTimedOut);
}

ERuneTransitionReason FRuneRuleStateMachine::HandleHit(int32 ArmorIndex, int32 RadiusMm, int32 Ring)
{
	Snapshot.LastHitArmorIndex = ArmorIndex;
	Snapshot.LastHitRadiusMm = RadiusMm;
	Snapshot.LastHitRing = Ring;

	if (ArmorIndex < 0 || ArmorIndex >= Config.RequiredGroups)
	{
		SetTransition(ERuneTransitionReason::InvalidHit);
		return Snapshot.LastTransition;
	}

	if (Snapshot.State != ERuneState::Activating)
	{
		SetTransition(ERuneTransitionReason::IgnoredInactiveHit);
		return Snapshot.LastTransition;
	}

	// A completed fan is permanently consumed for the current activation round.
	// Keep this guard at the hit boundary as well as in BeginRound so stale or
	// externally supplied target indices can never reactivate it.
	if (Snapshot.Mode == ESceneMode::SmallRune
		&& Snapshot.CompletedArmorIndices.Contains(ArmorIndex))
	{
		SetTransition(ERuneTransitionReason::IgnoredDuplicateHit);
		return Snapshot.LastTransition;
	}

	if (Snapshot.Phase == ERuneRoundPhase::AwaitSecondHit)
	{
		if (ArmorIndex == Snapshot.FirstHitArmorIndex)
		{
			SetTransition(ERuneTransitionReason::IgnoredDuplicateHit);
			return Snapshot.LastTransition;
		}
		if (Snapshot.SecondHitArmorIndex != INDEX_NONE)
		{
			SetTransition(ERuneTransitionReason::IgnoredDuplicateHit);
			return Snapshot.LastTransition;
		}
		if (!Snapshot.LitArmorIndices.Contains(ArmorIndex))
		{
			return Fail(ERuneTransitionReason::WrongTarget);
		}

		if (Snapshot.ArmorHitRings.IsValidIndex(ArmorIndex) && Ring >= 1 && Ring <= 10)
		{
			Snapshot.ArmorHitRings[ArmorIndex] = Ring;
		}
		Snapshot.SecondHitArmorIndex = ArmorIndex;
		Snapshot.LitArmorIndices.Remove(ArmorIndex);
		// The optional second large-rune hit only changes the current target
		// display. A group is completed by its first successful hit.
		SetTransition(ERuneTransitionReason::SecondHitSucceeded);
		return Snapshot.LastTransition;
	}

	if (!Snapshot.LitArmorIndices.Contains(ArmorIndex))
	{
		return Fail(ERuneTransitionReason::WrongTarget);
	}

	Snapshot.FirstHitArmorIndex = ArmorIndex;
	// A hit fan is no longer an active target in the current group. Small-rune
	// completion remains persistent; large-rune fan selection resets next group.
	Snapshot.LitArmorIndices.Remove(ArmorIndex);
	if (Snapshot.ArmorHitRings.IsValidIndex(ArmorIndex) && Ring >= 1 && Ring <= 10)
	{
		Snapshot.ArmorHitRings[ArmorIndex] = Ring;
	}
	if (Snapshot.Mode == ESceneMode::SmallRune)
	{
		Snapshot.CompletedArmorIndices.AddUnique(ArmorIndex);
		Snapshot.ActivatedGroups = Snapshot.CompletedArmorIndices.Num();
		SetTransition(ERuneTransitionReason::FirstHitSucceeded);
		return FinishRound();
	}

	// Large-rune progress counts successful groups, not unique physical fans.
	// Every new group may select a fan that appeared in an earlier group.
	++Snapshot.ActivatedGroups;
	if (Snapshot.ActivatedGroups >= Config.RequiredGroups)
	{
		return CompleteActivation();
	}
	Snapshot.Phase = ERuneRoundPhase::AwaitSecondHit;
	Snapshot.DeadlineSeconds = CurrentTimeSeconds + Config.SecondHitWindowSeconds;
	SetTransition(ERuneTransitionReason::FirstHitSucceeded);
	return Snapshot.LastTransition;
}

ERuneTransitionReason FRuneRuleStateMachine::BeginRound()
{
	Snapshot.LitArmorIndices.Reset();
	Snapshot.FirstHitArmorIndex = INDEX_NONE;
	Snapshot.SecondHitArmorIndex = INDEX_NONE;
	Snapshot.Phase = ERuneRoundPhase::AwaitFirstHit;
	Snapshot.DeadlineSeconds = CurrentTimeSeconds + Config.FirstHitTimeoutSeconds;

	TArray<int32> Candidates;
	for (int32 Index = 0; Index < Config.RequiredGroups; ++Index)
	{
		if (Snapshot.Mode == ESceneMode::LargeRune
			|| !Snapshot.CompletedArmorIndices.Contains(Index))
		{
			Candidates.Add(Index);
		}
	}

	if (Candidates.IsEmpty())
	{
		return CompleteActivation();
	}

	const int32 First = PickIndex(Candidates);
	Snapshot.LitArmorIndices.Add(First);
	if (Snapshot.Mode == ESceneMode::LargeRune && Candidates.Num() > 1)
	{
		Candidates.Remove(First);
		Snapshot.LitArmorIndices.Add(PickIndex(Candidates));
	}

	SetTransition(ERuneTransitionReason::RoundStarted);
	return Snapshot.LastTransition;
}

ERuneTransitionReason FRuneRuleStateMachine::FinishRound()
{
	if (Snapshot.ActivatedGroups >= Config.RequiredGroups)
	{
		return CompleteActivation();
	}

	BeginRound();
	SetTransition(ERuneTransitionReason::RoundCompleted);
	return Snapshot.LastTransition;
}

ERuneTransitionReason FRuneRuleStateMachine::CompleteActivation()
{
	Snapshot.State = ERuneState::Activated;
	Snapshot.Phase = ERuneRoundPhase::None;
	Snapshot.LitArmorIndices.Reset();
	CompletionStartedSeconds = CurrentTimeSeconds;
	Snapshot.DeadlineSeconds = CurrentTimeSeconds + Config.CompletionFlashSeconds;
	Snapshot.bCompletionFlashOn = true;
	SetTransition(ERuneTransitionReason::ActivationCompleted);
	return Snapshot.LastTransition;
}

ERuneTransitionReason FRuneRuleStateMachine::Fail(ERuneTransitionReason Reason)
{
	Snapshot.State = ERuneState::ActivationFailed;
	Snapshot.Phase = ERuneRoundPhase::None;
	Snapshot.LitArmorIndices.Reset();
	// A failed hit starts a fresh activation attempt. Clear progress now, not
	// only when the failure hold expires, so the visual layer cannot keep
	// showing materials from the failed attempt.
	Snapshot.CompletedArmorIndices.Reset();
	Snapshot.ArmorHitRings.Init(0, Config.RequiredGroups);
	Snapshot.ActivatedGroups = 0;
	Snapshot.FirstHitArmorIndex = INDEX_NONE;
	Snapshot.SecondHitArmorIndex = INDEX_NONE;
	Snapshot.DeadlineSeconds = CurrentTimeSeconds + Config.FailureHoldSeconds;
	SetTransition(Reason);
	return Snapshot.LastTransition;
}

void FRuneRuleStateMachine::SetTransition(ERuneTransitionReason Reason)
{
	Snapshot.LastTransition = Reason;
}

int32 FRuneRuleStateMachine::PickIndex(const TArray<int32>& Candidates)
{
	check(!Candidates.IsEmpty());
	return Candidates[Random.RandRange(0, Candidates.Num() - 1)];
}
