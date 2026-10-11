#if WITH_DEV_AUTOMATION_TESTS

#include "Misc/AutomationTest.h"
#include "RuneVisualRules.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FRuneArmLightVisualRulesTest,
	"rm_sim.Rune.Visual.ArmLightMaterials",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FRuneArmLightVisualRulesTest::RunTest(const FString& Parameters)
{
	FRuneSnapshot Snapshot;
	Snapshot.State = ERuneState::Activating;
	Snapshot.LitArmorIndices = {1};
	Snapshot.CompletedArmorIndices = {2};

	const FRuneArmLightVisualState Target = RuneVisualRules::ResolveArmLights(Snapshot, 1);
	TestEqual(TEXT("Target horizontal lights stay metal"), Target.Horizontal, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Target vertical lights stay metal"), Target.Vertical, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Only target flow uses arrow material"), Target.Flow, ERuneResolvedLightMaterial::Arrow);

	const FRuneArmLightVisualState Completed = RuneVisualRules::ResolveArmLights(Snapshot, 2);
	TestEqual(TEXT("Completed horizontal lights use active material"), Completed.Horizontal, ERuneResolvedLightMaterial::Active);
	TestEqual(TEXT("Completed vertical lights stay metal"), Completed.Vertical, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Completed flow uses active material"), Completed.Flow, ERuneResolvedLightMaterial::Active);

	const FRuneArmLightVisualState Unrelated = RuneVisualRules::ResolveArmLights(Snapshot, 3);
	TestEqual(TEXT("Unrelated horizontal lights stay metal"), Unrelated.Horizontal, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Unrelated vertical lights stay metal"), Unrelated.Vertical, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Unrelated flow stays metal"), Unrelated.Flow, ERuneResolvedLightMaterial::Metal);

	Snapshot.Mode = ESceneMode::LargeRune;
	Snapshot.State = ERuneState::Activating;
	Snapshot.LitArmorIndices = {1, 2};
	Snapshot.CompletedArmorIndices.Reset();
	const FRuneArmLightVisualState LargeTarget = RuneVisualRules::ResolveArmLights(Snapshot, 1);
	TestEqual(TEXT("Large target horizontal lights stay active"), LargeTarget.Horizontal, ERuneResolvedLightMaterial::Active);
	TestEqual(TEXT("Large target flow uses arrow"), LargeTarget.Flow, ERuneResolvedLightMaterial::Arrow);
	const FRuneArmLightVisualState LargeUnlit = RuneVisualRules::ResolveArmLights(Snapshot, 3);
	TestEqual(TEXT("Large unlit horizontal lights stay active"), LargeUnlit.Horizontal, ERuneResolvedLightMaterial::Active);
	TestEqual(TEXT("Large unlit flow stays active"), LargeUnlit.Flow, ERuneResolvedLightMaterial::Active);
	TestEqual(TEXT("Large vertical lights stay metal"), LargeUnlit.Vertical, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Large target fan uses active target material"),
		RuneVisualRules::ResolveFan(Snapshot, 1), ERuneResolvedFanMaterial::Active);
	Snapshot.LitArmorIndices.Remove(1);
	Snapshot.ArmorHitRings.Init(0, 5);
	Snapshot.ArmorHitRings[1] = 10;
	TestEqual(TEXT("Hit large-rune fan goes metal even with a recorded ring"),
		RuneVisualRules::ResolveFan(Snapshot, 1), ERuneResolvedFanMaterial::Metal);
	Snapshot.State = ERuneState::ActivationFailed;
	TestEqual(TEXT("Failed activation resets fan target material"),
		RuneVisualRules::ResolveFan(Snapshot, 2), ERuneResolvedFanMaterial::Metal);

	Snapshot.State = ERuneState::Activated;
	Snapshot.bCompletionFlashOn = true;
	const FRuneArmLightVisualState FlashOn = RuneVisualRules::ResolveArmLights(Snapshot, 3);
	TestEqual(TEXT("Completion flash lights horizontal with full"), FlashOn.Horizontal, ERuneResolvedLightMaterial::Full);
	TestEqual(TEXT("Completion flash lights vertical with full"), FlashOn.Vertical, ERuneResolvedLightMaterial::Full);
	TestEqual(TEXT("Completion flash lights flow with full"), FlashOn.Flow, ERuneResolvedLightMaterial::Full);

	Snapshot.bCompletionFlashOn = false;
	const FRuneArmLightVisualState FlashOff = RuneVisualRules::ResolveArmLights(Snapshot, 2);
	TestEqual(TEXT("Completion dark phase uses metal horizontally"), FlashOff.Horizontal, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Completion dark phase uses metal vertically"), FlashOff.Vertical, ERuneResolvedLightMaterial::Metal);
	TestEqual(TEXT("Completion dark phase uses metal for flow"), FlashOff.Flow, ERuneResolvedLightMaterial::Metal);
	return true;
}

#endif
