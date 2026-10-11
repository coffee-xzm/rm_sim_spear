#include "RuneArmorTargetComponent.h"

#include "Rune.h"

void URuneArmorTargetComponent::InitializeTarget(
	ARune* InRune,
	ETeamColor InTeam,
	int32 InArmorIndex,
	double RadiusCm,
	double ThicknessCm)
{
	if (!InRune || InArmorIndex < 0 || InArmorIndex >= 5 || RadiusCm <= 0.0 || ThicknessCm <= 0.0)
	{
		Rune.Reset();
		ArmorIndex = INDEX_NONE;
		TargetRadiusCm = 0.0;
		SetCollisionEnabled(ECollisionEnabled::NoCollision);
		return;
	}
	Rune = InRune;
	TargetTeam = InTeam;
	ArmorIndex = InArmorIndex;
	TargetRadiusCm = RadiusCm;
	SetBoxExtent(FVector(RadiusCm, RadiusCm, ThicknessCm * 0.5));
	SetCollisionProfileName(TEXT("RuneArmor"));
	SetGenerateOverlapEvents(false);
}
