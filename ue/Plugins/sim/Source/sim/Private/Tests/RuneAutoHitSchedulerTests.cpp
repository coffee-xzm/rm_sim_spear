#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "RuneAutoHitScheduler.h"
#include "RuneRuleStateMachine.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneAutoHitSchedulerTest,
	"rm_sim.Rune.Test.AutoHitAfterOneSecond",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneAutoHitSchedulerTest::RunTest(const FString& Parameters)
{
	for (const ESceneMode Mode : {ESceneMode::SmallRune, ESceneMode::LargeRune})
	{
		FRuneRuleStateMachine Rules;
		FRuneAutoHitScheduler Scheduler;
		Rules.Reset(Mode, 2026);
		const int32 ExpectedTarget = Rules.GetSnapshot().LitArmorIndices[0];

		TestEqual(TEXT("Disabled scheduler never hits"),
			Scheduler.Advance(Rules.GetSnapshot(), 2.0, false, 1.0), INDEX_NONE);
		TestEqual(TEXT("No automatic hit before one second"),
			Scheduler.Advance(Rules.GetSnapshot(), 0.999, true, 1.0), INDEX_NONE);
		const int32 AutoHit = Scheduler.Advance(Rules.GetSnapshot(), 0.001, true, 1.0);
		TestEqual(TEXT("One lit armor is selected after one second"), AutoHit, ExpectedTarget);

		Rules.HandleHit(AutoHit, 0, 10);
		if (Mode == ESceneMode::SmallRune)
		{
			TestTrue(TEXT("Small-rune automatic target is recorded as completed"),
				Rules.GetSnapshot().CompletedArmorIndices.Contains(AutoHit));
		}
		else
		{
			TestEqual(TEXT("Large-rune automatic hit completes one group"),
				Rules.GetSnapshot().ActivatedGroups, 1);
			TestEqual(TEXT("Large-rune fan is reusable in later groups"),
				Rules.GetSnapshot().CompletedArmorIndices.Num(), 0);
		}
	}
	return true;
}

#endif
