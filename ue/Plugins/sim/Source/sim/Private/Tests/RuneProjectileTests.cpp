#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "RunePlayerController.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneProjectileTransformTest,
	"rm_sim.Rune.Projectile.ShotTransform",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneProjectileTransformTest::RunTest(const FString& Parameters)
{
	const FTransform Camera(FRotator(0.0, 90.0, 0.0), FVector(100.0, 200.0, 300.0));
	const FTransform ShotFrame(FRotator::ZeroRotator, FVector(100.0, 0.0, 0.0));
	const FTransform Result = ARunePlayerController::ComposeShotTransform(Camera, ShotFrame);
	TestTrue(TEXT("Camera-local forward offset is rotated into world space"),
		Result.GetLocation().Equals(FVector(100.0, 300.0, 300.0), 0.01));
	TestTrue(TEXT("Camera rotation is preserved"),
		Result.GetRotation().Equals(Camera.GetRotation(), 1e-6));
	return true;
}

#endif
