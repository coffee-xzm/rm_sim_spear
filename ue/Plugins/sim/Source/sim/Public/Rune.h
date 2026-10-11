#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "RuneAutoHitScheduler.h"
#include "RuneBindingTypes.h"
#include "RuneRuleStateMachine.h"
#include "RuneTypes.h"
#include "Rune.generated.h"

class URuneArmorTargetComponent;
class URuneVisualProfile;
class UMaterialInterface;
class UStaticMeshComponent;

USTRUCT()
struct FRuneVisualSlot
{
	GENERATED_BODY()

	UPROPERTY(Transient)
	TObjectPtr<UStaticMeshComponent> Component = nullptr;

	UPROPERTY(Transient)
	int32 MaterialIndex = INDEX_NONE;

	UPROPERTY(Transient)
	TObjectPtr<UMaterialInterface> OriginalMaterial = nullptr;

};

USTRUCT()
struct FRuneArmVisualSlots
{
	GENERATED_BODY()

	UPROPERTY(Transient)
	TArray<FRuneVisualSlot> FanSlots;

	UPROPERTY(Transient)
	TArray<FRuneVisualSlot> FlowSlots;

	UPROPERTY(Transient)
	TArray<FRuneVisualSlot> HorizontalLightSlots;

	UPROPERTY(Transient)
	TArray<FRuneVisualSlot> VerticalLightSlots;
};

DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams(FOnRuneStateChanged, ERuneState, NewState, ERuneState, OldState);
DECLARE_DYNAMIC_MULTICAST_DELEGATE_ThreeParams(FOnArmorHit, int32, ArmorIndex, int32, RingNumber, ETeamColor, HitByTeam);

UCLASS()
class SIM_API ARune : public AActor
{
	GENERATED_BODY()

public:
	ARune();

	virtual void BeginPlay() override;

	void InitializeRules(ESceneMode Mode, int32 Seed);
	void AdvanceRules(double DeltaSeconds, bool bAutoHitTestEnabled = false, double AutoHitDelaySeconds = 1.0);
	ERuneTransitionReason HandleArmorHit(int32 ArmorIndex, const FVector& ImpactPoint, ETeamColor ShooterTeam);
	void ApplySharedAngle(double AngleRad);
	void DrawDebugTargets() const;

	UFUNCTION(BlueprintPure, Category = "Rune")
	const FRuneSnapshot& GetSnapshot() const { return RuleState.GetSnapshot(); }

	UFUNCTION(BlueprintCallable, Category = "Rune")
	void RefreshVisuals();

	bool ValidateBinding(TArray<FString>& OutErrors) const;
	bool ValidateVisualProfile(TArray<FString>& OutErrors) const;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, Category = "Rune|Config")
	ETeamColor TeamColor = ETeamColor::Red;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, Category = "Rune|Binding")
	FRuneSideBinding Binding;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Visual")
	TObjectPtr<URuneVisualProfile> VisualProfile = nullptr;

	UPROPERTY(BlueprintAssignable, Category = "Rune|Event")
	FOnRuneStateChanged OnRuneStateChanged;

	UPROPERTY(BlueprintAssignable, Category = "Rune|Event")
	FOnArmorHit OnArmorHit;

private:
	void ConfigureHitTargets();
	void InitializeVisualSlots();
	void CollectVisualSlots(AActor* Actor, bool bRecursive, TArray<FRuneVisualSlot>& OutSlots);
	void ApplyMaterialToSlots(TArray<FRuneVisualSlot>& Slots, UMaterialInterface* Material, bool bFirstMaterialSlotOnly = false);
	void WarnForMissingVisualMaterials();
	void SetVisualCollisionResponse(AActor* Actor) const;
	UMaterialInterface* GetActiveMaterial() const;
	UMaterialInterface* GetCenterMaterial(bool bInactive) const;
	UMaterialInterface* GetFanActiveMaterial() const;
	UMaterialInterface* GetFullMaterial() const;
	UMaterialInterface* GetArrowMaterial() const;
	UMaterialInterface* GetFanRingMaterial(int32 Ring) const;

	UPROPERTY(VisibleAnywhere, Category = "Rune|Hit")
	TArray<TObjectPtr<URuneArmorTargetComponent>> ArmorTargets;

	UPROPERTY(Transient)
	TArray<FRuneVisualSlot> RootVisualSlots;

	UPROPERTY(Transient)
	TArray<FRuneVisualSlot> CenterVisualSlots;

	UPROPERTY(Transient)
	TArray<FRuneArmVisualSlots> ArmVisualSlots;

	FRuneRuleStateMachine RuleState;
	FRuneAutoHitScheduler AutoHitScheduler;
	ERuneState LastBroadcastState = ERuneState::Inactive;
	bool bVisualWarningsEmitted = false;
};
