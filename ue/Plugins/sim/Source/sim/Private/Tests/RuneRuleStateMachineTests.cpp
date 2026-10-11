#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "RuneRuleStateMachine.h"

namespace
{
	TArray<int32> CollectSmallSequence(int32 Seed)
	{
		FRuneRuleStateMachine Rules;
		Rules.Reset(ESceneMode::SmallRune, Seed);
		TArray<int32> Sequence;
		while (Rules.GetSnapshot().State == ERuneState::Activating)
		{
			const int32 Target = Rules.GetSnapshot().LitArmorIndices[0];
			Sequence.Add(Target);
			Rules.HandleHit(Target);
		}
		return Sequence;
	}
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneSmallActivationTest,
	"rm_sim.Rune.Rules.SmallActivation",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneSmallActivationTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine Rules;
	Rules.Reset(ESceneMode::SmallRune, 1234);
	TSet<int32> Selected;

	for (int32 Round = 0; Round < 5; ++Round)
	{
		const FRuneSnapshot& Before = Rules.GetSnapshot();
		TestEqual(TEXT("Small rune exposes one target"), Before.LitArmorIndices.Num(), 1);
		const int32 Target = Before.LitArmorIndices[0];
		Selected.Add(Target);
		Rules.HandleHit(Target, 10 + Round, Round + 1);
		TestEqual(TEXT("Small rune stores the hit ring per arm"),
			Rules.GetSnapshot().ArmorHitRings[Target], Round + 1);
	}

	TestEqual(TEXT("All five physical arms were selected"), Selected.Num(), 5);
	TestEqual(TEXT("Small rune activates"), Rules.GetSnapshot().State, ERuneState::Activated);
	TestEqual(TEXT("Progress remains five"), Rules.GetSnapshot().ActivatedGroups, 5);
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneCompletedFanCannotBeHitAgainTest,
	"rm_sim.Rune.Rules.CompletedFanCannotBeHitAgain",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneCompletedFanCannotBeHitAgainTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine Rules;
	Rules.Reset(ESceneMode::SmallRune, 123);
	const int32 Completed = Rules.GetSnapshot().LitArmorIndices[0];
	Rules.HandleHit(Completed, 20, 4);
	TestTrue(TEXT("Completed fan is recorded"), Rules.GetSnapshot().CompletedArmorIndices.Contains(Completed));
	TestFalse(TEXT("Completed fan is removed from active targets"), Rules.GetSnapshot().LitArmorIndices.Contains(Completed));
	TestEqual(TEXT("A completed fan hit is ignored"), Rules.HandleHit(Completed, 20, 4),
		ERuneTransitionReason::IgnoredDuplicateHit);
	TestEqual(TEXT("Duplicate hit does not change progress"), Rules.GetSnapshot().ActivatedGroups, 1);
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneSmallFailureTest,
	"rm_sim.Rune.Rules.SmallFailureRestart",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneSmallFailureTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine Rules;
	Rules.Reset(ESceneMode::SmallRune, 17);
	Rules.HandleHit(Rules.GetSnapshot().LitArmorIndices[0], 20, 7);
	const int32 Target = Rules.GetSnapshot().LitArmorIndices[0];
	int32 WrongTarget = INDEX_NONE;
	for (int32 Candidate = 0; Candidate < 5; ++Candidate)
	{
		if (!Rules.GetSnapshot().LitArmorIndices.Contains(Candidate)
			&& !Rules.GetSnapshot().CompletedArmorIndices.Contains(Candidate))
		{
			WrongTarget = Candidate;
			break;
		}
	}
	TestTrue(TEXT("A distinct unfinished wrong target exists"), WrongTarget != INDEX_NONE);
	Rules.HandleHit(WrongTarget);
	TestEqual(TEXT("Wrong target fails"), Rules.GetSnapshot().State, ERuneState::ActivationFailed);
	TestEqual(TEXT("Failure clears progress immediately"), Rules.GetSnapshot().ActivatedGroups, 0);
	TestEqual(TEXT("Failure clears completed arms immediately"), Rules.GetSnapshot().CompletedArmorIndices.Num(), 0);
	TestTrue(TEXT("Failure clears fan ring materials immediately"),
		Rules.GetSnapshot().ArmorHitRings.FilterByPredicate([](int32 Ring) { return Ring != 0; }).IsEmpty());

	Rules.AdvanceTime(0.499);
	TestEqual(TEXT("Failure hold lasts for 0.5 seconds"), Rules.GetSnapshot().State, ERuneState::ActivationFailed);
	Rules.AdvanceTime(0.001);
	TestEqual(TEXT("Failure automatically restarts"), Rules.GetSnapshot().State, ERuneState::Activating);
	TestEqual(TEXT("Restart clears progress"), Rules.GetSnapshot().ActivatedGroups, 0);
	TestEqual(TEXT("Restart clears completed arms"), Rules.GetSnapshot().CompletedArmorIndices.Num(), 0);
	TestTrue(TEXT("Restart clears per-arm hit rings"),
		Rules.GetSnapshot().ArmorHitRings.FilterByPredicate([](int32 Ring) { return Ring != 0; }).IsEmpty());
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneFirstHitDeadlineTest,
	"rm_sim.Rune.Rules.FirstHitDeadline",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneFirstHitDeadlineTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine BeforeDeadline;
	BeforeDeadline.Reset(ESceneMode::SmallRune, 41);
	const int32 Target = BeforeDeadline.GetSnapshot().LitArmorIndices[0];
	BeforeDeadline.AdvanceTime(2.499);
	TestEqual(TEXT("Hit immediately before deadline completes the round"),
		BeforeDeadline.HandleHit(Target), ERuneTransitionReason::RoundCompleted);
	TestEqual(TEXT("Hit immediately before deadline increments progress"),
		BeforeDeadline.GetSnapshot().ActivatedGroups, 1);

	FRuneRuleStateMachine AtDeadline;
	AtDeadline.Reset(ESceneMode::SmallRune, 41);
	AtDeadline.AdvanceTime(2.5);
	TestEqual(TEXT("Deadline itself times out"), AtDeadline.GetSnapshot().State, ERuneState::ActivationFailed);
	TestEqual(TEXT("Timeout reason is retained"), AtDeadline.GetSnapshot().LastTransition,
		ERuneTransitionReason::FirstHitTimedOut);
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneLargeWindowTest,
	"rm_sim.Rune.Rules.LargeSecondHitWindow",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneLargeWindowTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine Rules;
	Rules.Reset(ESceneMode::LargeRune, 88);
	const TArray<int32> FirstTargets = Rules.GetSnapshot().LitArmorIndices;
	TestEqual(TEXT("Large rune exposes two targets"), FirstTargets.Num(), 2);
	TestNotEqual(TEXT("Targets are distinct"), FirstTargets[0], FirstTargets[1]);

	Rules.HandleHit(FirstTargets[0], 20, 7);
	TestEqual(TEXT("First hit increments group"), Rules.GetSnapshot().ActivatedGroups, 1);
	TestEqual(TEXT("Second-hit phase starts"), Rules.GetSnapshot().Phase, ERuneRoundPhase::AwaitSecondHit);
	TestFalse(TEXT("First completed target is no longer active"),
		Rules.GetSnapshot().LitArmorIndices.Contains(FirstTargets[0]));
	Rules.HandleHit(FirstTargets[0]);
	TestEqual(TEXT("Duplicate first target is ignored"), Rules.GetSnapshot().LastTransition,
		ERuneTransitionReason::IgnoredDuplicateHit);
	Rules.HandleHit(FirstTargets[1], 35, 4);
	TestEqual(TEXT("Second target is recorded"), Rules.GetSnapshot().SecondHitArmorIndex, FirstTargets[1]);
	TestEqual(TEXT("First target ring is recorded"), Rules.GetSnapshot().ArmorHitRings[FirstTargets[0]], 7);
	TestEqual(TEXT("Second target ring is recorded"), Rules.GetSnapshot().ArmorHitRings[FirstTargets[1]], 4);
	TestEqual(TEXT("Second hit does not end the one-second window"), Rules.GetSnapshot().Phase,
		ERuneRoundPhase::AwaitSecondHit);
	Rules.AdvanceTime(0.999);
	TestEqual(TEXT("Window remains open before one second"), Rules.GetSnapshot().Phase,
		ERuneRoundPhase::AwaitSecondHit);
	Rules.AdvanceTime(0.001);
	TestEqual(TEXT("Next round starts when window ends"), Rules.GetSnapshot().Phase,
		ERuneRoundPhase::AwaitFirstHit);
	TestEqual(TEXT("Optional second hit does not add group progress"), Rules.GetSnapshot().ActivatedGroups, 1);
	TestEqual(TEXT("Large rune does not permanently consume physical fans"),
		Rules.GetSnapshot().CompletedArmorIndices.Num(), 0);

	int32 SuccessfulGroups = 1;
	while (Rules.GetSnapshot().State == ERuneState::Activating)
	{
		const TArray<int32> Targets = Rules.GetSnapshot().LitArmorIndices;
		TestEqual(TEXT("Each large-rune group exposes two targets"), Targets.Num(), 2);
		Rules.HandleHit(Targets[0], 20, 3);
		++SuccessfulGroups;
		TestEqual(TEXT("Only the first hit increments large-rune progress"),
			Rules.GetSnapshot().ActivatedGroups, SuccessfulGroups);
		if (Rules.GetSnapshot().State == ERuneState::Activating
			&& Rules.GetSnapshot().Phase == ERuneRoundPhase::AwaitSecondHit
			&& Targets.Num() > 1)
		{
			Rules.HandleHit(Targets[1], 25, 4);
			TestEqual(TEXT("Optional second hit leaves progress unchanged"),
				Rules.GetSnapshot().ActivatedGroups, SuccessfulGroups);
		}
		if (Rules.GetSnapshot().State == ERuneState::Activating
			&& Rules.GetSnapshot().Phase == ERuneRoundPhase::AwaitSecondHit)
		{
			Rules.AdvanceTime(1.0);
		}
	}

	TestEqual(TEXT("Large rune activates after five successful groups"), Rules.GetSnapshot().State, ERuneState::Activated);
	TestEqual(TEXT("Exactly five successful groups are required"), SuccessfulGroups, 5);
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneCompletionFlashTest,
	"rm_sim.Rune.Rules.CompletionFlash",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneCompletionFlashTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine Rules;
	Rules.Reset(ESceneMode::SmallRune, 31);
	while (Rules.GetSnapshot().State == ERuneState::Activating)
	{
		Rules.HandleHit(Rules.GetSnapshot().LitArmorIndices[0], 20, 2);
	}
	TestTrue(TEXT("Completion display starts with Full material phase"), Rules.GetSnapshot().bCompletionFlashOn);
	Rules.AdvanceTime(0.15);
	TestFalse(TEXT("Completion display switches to Metal phase"), Rules.GetSnapshot().bCompletionFlashOn);
	Rules.AdvanceTime(0.15);
	TestTrue(TEXT("Completion display switches back to Full phase"), Rules.GetSnapshot().bCompletionFlashOn);
	Rules.AdvanceTime(1.2);
	TestEqual(TEXT("Completion display returns to inactive after 1.5 seconds"), Rules.GetSnapshot().State, ERuneState::Inactive);
	TestEqual(TEXT("Inactive reset clears completed fans"), Rules.GetSnapshot().CompletedArmorIndices.Num(), 0);
	TestEqual(TEXT("Inactive reset clears progress"), Rules.GetSnapshot().ActivatedGroups, 0);
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneLargeFailureTest,
	"rm_sim.Rune.Rules.LargeFailureCases",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneLargeFailureTest::RunTest(const FString& Parameters)
{
	FRuneRuleStateMachine WrongFirst;
	WrongFirst.Reset(ESceneMode::LargeRune, 9);
	const TArray<int32> Targets = WrongFirst.GetSnapshot().LitArmorIndices;
	int32 WrongTarget = 0;
	while (Targets.Contains(WrongTarget))
	{
		++WrongTarget;
	}
	WrongFirst.HandleHit(WrongTarget);
	TestEqual(TEXT("Wrong first target fails"), WrongFirst.GetSnapshot().LastTransition,
		ERuneTransitionReason::WrongTarget);

	FRuneRuleStateMachine WrongSecond;
	WrongSecond.Reset(ESceneMode::LargeRune, 9);
	const TArray<int32> SecondTargets = WrongSecond.GetSnapshot().LitArmorIndices;
	WrongSecond.HandleHit(SecondTargets[0]);
	WrongTarget = 0;
	while (SecondTargets.Contains(WrongTarget))
	{
		++WrongTarget;
	}
	WrongSecond.HandleHit(WrongTarget);
	TestEqual(TEXT("Wrong second target fails"), WrongSecond.GetSnapshot().LastTransition,
		ERuneTransitionReason::WrongTarget);
	return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneModeAndSeedTest,
	"rm_sim.Rune.Rules.ModeAndSeed",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneModeAndSeedTest::RunTest(const FString& Parameters)
{
	const TArray<int32> First = CollectSmallSequence(1234);
	const TArray<int32> Same = CollectSmallSequence(1234);
	const TArray<int32> Different = CollectSmallSequence(5678);
	TestEqual(TEXT("Same seed yields the same target sequence"), First, Same);
	TestNotEqual(TEXT("Chosen different seeds yield different sequences"), First, Different);

	FRuneRuleStateMachine Rules;
	Rules.Reset(ESceneMode::SmallRune, 12);
	Rules.AdvanceTime(1.0);
	Rules.SetMode(ESceneMode::Inactive);
	TestEqual(TEXT("Mode switch enters inactive state"), Rules.GetSnapshot().State, ERuneState::Inactive);
	TestEqual(TEXT("Mode switch clears deadline"), Rules.GetSnapshot().DeadlineSeconds, 0.0);
	Rules.SetMode(ESceneMode::LargeRune);
	TestEqual(TEXT("Mode switch starts large activation"), Rules.GetSnapshot().Mode, ESceneMode::LargeRune);
	TestEqual(TEXT("Mode switch resets progress"), Rules.GetSnapshot().ActivatedGroups, 0);
	TestEqual(TEXT("Large mode starts with two targets"), Rules.GetSnapshot().LitArmorIndices.Num(), 2);
	return true;
}

#endif
