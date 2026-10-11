#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "RuneHitMath.h"

#include <limits>

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneHitMathTest,
	"rm_sim.Rune.HitMath.RingBoundaries",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneHitMathTest::RunTest(const FString& Parameters)
{
	struct FCase
	{
		int32 RadiusMm;
		bool bValid;
		int32 Ring;
	};

	const FCase Cases[] = {
		{0, true, 10}, {4, true, 10}, {5, true, 9}, {44, true, 2},
		{45, true, 1}, {49, true, 1}, {50, true, 1}, {51, false, 0},
	};
	const FTransform Transform(FRotator(20.0, 30.0, 10.0), FVector(100.0, -20.0, 50.0));

	for (const FCase& Case : Cases)
	{
		const FVector LocalPoint(Case.RadiusMm / 10.0, 0.0, 0.0);
		const FRuneHitCalculation Result = RuneHitMath::Calculate(Transform, Transform.TransformPosition(LocalPoint));
		TestEqual(FString::Printf(TEXT("Validity at %d mm"), Case.RadiusMm), Result.bValid, Case.bValid);
		TestEqual(FString::Printf(TEXT("Ring at %d mm"), Case.RadiusMm), Result.Ring, Case.Ring);
	}

	const FTransform Mirrored(FRotator(-35.0, 140.0, 5.0), FVector(-80.0, 250.0, 10.0));
	const FRuneHitCalculation MirroredResult = RuneHitMath::Calculate(
		Mirrored, Mirrored.TransformPosition(FVector(-2.5, 0.0, 0.0)));
	TestTrue(TEXT("Mirrored target hit is valid"), MirroredResult.bValid);
	TestEqual(TEXT("Mirrored target keeps radial distance"), MirroredResult.RadiusMm, 25);
	TestEqual(TEXT("Mirrored target ring"), MirroredResult.Ring, 5);

	const FRuneHitCalculation OffPlane = RuneHitMath::Calculate(
		Transform, Transform.TransformPosition(FVector(1.0, 2.0, 100.0)));
	TestTrue(TEXT("Normal-axis displacement does not change radial validity"), OffPlane.bValid);
	TestEqual(TEXT("Normal-axis displacement uses the XY target plane"), OffPlane.RadiusMm, 22);
	TestFalse(TEXT("Zero target radius is invalid"), RuneHitMath::Calculate(Transform, FVector::ZeroVector, 0.0).bValid);
	TestFalse(TEXT("Negative target radius is invalid"), RuneHitMath::Calculate(Transform, FVector::ZeroVector, -1.0).bValid);
	TestFalse(TEXT("NaN impact point is invalid"),
		RuneHitMath::Calculate(Transform, FVector(std::numeric_limits<double>::quiet_NaN(), 0.0, 0.0)).bValid);
	return true;
}

#endif
