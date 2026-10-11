#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "RuneMotionModel.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneMotionModelTest,
	"rm_sim.Rune.Motion.SharedModel",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneMotionModelTest::RunTest(const FString& Parameters)
{
	constexpr double ExpectedFixedSpeed = UE_PI / 3.0;
	FRuneMotionModel Motion;
	Motion.BeginMatch(42);
	Motion.SetMode(ESceneMode::LargeRune);
	const FRuneMotionSnapshot Initial = Motion.GetSnapshot();
	TestTrue(TEXT("a is in range"), Initial.A >= 0.780 && Initial.A <= 1.045);
	TestTrue(TEXT("omega is in range"), Initial.Omega >= 1.884 && Initial.Omega <= 2.000);
	TestTrue(TEXT("b follows invariant"), FMath::IsNearlyEqual(Initial.B, 2.090 - Initial.A, 1e-6));

	FRuneMotionModel OneStep;
	OneStep.BeginMatch(42);
	OneStep.SetMode(ESceneMode::LargeRune);
	OneStep.AdvanceTime(10.0, true);

	FRuneMotionModel ManySteps;
	ManySteps.BeginMatch(42);
	ManySteps.SetMode(ESceneMode::LargeRune);
	for (int32 Index = 0; Index < 1000; ++Index)
	{
		ManySteps.AdvanceTime(0.01, true);
	}

	TestTrue(TEXT("Analytic integration is frame independent"),
		FMath::IsNearlyEqual(OneStep.GetSnapshot().AngleRad, ManySteps.GetSnapshot().AngleRad, 1e-6));

	FRuneMotionModel SameSeed;
	SameSeed.BeginMatch(42);
	SameSeed.SetMode(ESceneMode::LargeRune);
	TestEqual(TEXT("Same seed keeps direction"), SameSeed.GetSnapshot().Direction, Initial.Direction);
	TestTrue(TEXT("Same seed keeps A"), FMath::IsNearlyEqual(SameSeed.GetSnapshot().A, Initial.A));
	TestTrue(TEXT("Same seed keeps omega"), FMath::IsNearlyEqual(SameSeed.GetSnapshot().Omega, Initial.Omega));

	FRuneMotionModel Fixed;
	Fixed.BeginMatch(9);
	Fixed.SetMode(ESceneMode::Inactive);
	Fixed.AdvanceTime(1.0, true);
	TestTrue(TEXT("Inactive mode uses fixed speed"),
		FMath::IsNearlyEqual(Fixed.GetSnapshot().SpeedRadPerSecond, ExpectedFixedSpeed));
	TestTrue(TEXT("Inactive mode advances by fixed speed"),
		FMath::IsNearlyEqual(FMath::Abs(Fixed.GetSnapshot().AngleRad), ExpectedFixedSpeed));
	Fixed.AdvanceTime(-10.0, true);
	TestTrue(TEXT("Negative delta is clamped"),
		FMath::IsNearlyEqual(FMath::Abs(Fixed.GetSnapshot().AngleRad), ExpectedFixedSpeed));

	FRuneMotionModel PausedLarge;
	PausedLarge.BeginMatch(11);
	PausedLarge.SetMode(ESceneMode::LargeRune);
	PausedLarge.AdvanceTime(0.75, false);
	TestTrue(TEXT("Inactive large rune uses fixed speed"),
		FMath::IsNearlyEqual(PausedLarge.GetSnapshot().SpeedRadPerSecond, ExpectedFixedSpeed));
	return true;
}

#endif
