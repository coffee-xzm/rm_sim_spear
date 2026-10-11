#pragma once

#include "CoreMinimal.h"
#include "Components/BoxComponent.h"
#include "RuneTypes.h"
#include "RuneArmorTargetComponent.generated.h"

class ARune;

UCLASS(ClassGroup = (Rune), meta = (BlueprintSpawnableComponent))
class SIM_API URuneArmorTargetComponent : public UBoxComponent
{
	GENERATED_BODY()

public:
	void InitializeTarget(ARune* InRune, ETeamColor InTeam, int32 InArmorIndex, double RadiusCm, double ThicknessCm);

	ARune* GetRune() const { return Rune.Get(); }
	int32 GetArmorIndex() const { return ArmorIndex; }
	ETeamColor GetTargetTeam() const { return TargetTeam; }
	double GetTargetRadiusCm() const { return TargetRadiusCm; }
	bool IsTargetValid() const { return Rune.IsValid() && ArmorIndex >= 0 && ArmorIndex < 5 && TargetRadiusCm > 0.0; }

private:
	TWeakObjectPtr<ARune> Rune;

	UPROPERTY(VisibleAnywhere, Category = "Rune")
	int32 ArmorIndex = INDEX_NONE;

	UPROPERTY(VisibleAnywhere, Category = "Rune")
	ETeamColor TargetTeam = ETeamColor::Red;

	UPROPERTY(VisibleAnywhere, Category = "Rune")
	double TargetRadiusCm = 5.0;
};
