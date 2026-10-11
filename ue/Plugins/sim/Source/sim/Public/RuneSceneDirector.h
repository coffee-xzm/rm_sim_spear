#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "RuneMotionModel.h"
#include "RuneSnapshot.h"
#include "RuneSceneDirector.generated.h"

class ARune;

UCLASS()
class SIM_API ARuneSceneDirector : public AActor
{
	GENERATED_BODY()

public:
	ARuneSceneDirector();

	virtual void BeginPlay() override;
	virtual void Tick(float DeltaSeconds) override;

	UFUNCTION(BlueprintCallable, Category = "RMSim")
	bool SetSceneMode(ESceneMode NewMode, int32 Seed);

	UFUNCTION(BlueprintCallable, Category = "RMSim")
	bool ResetMatch(int32 Seed);

	UFUNCTION(BlueprintPure, Category = "RMSim")
	FRunePairSnapshot GetSnapshot() const;
	void SetDebugOverlay(bool bEnabled) { bDebugOverlay = bEnabled; }
	bool IsDebugOverlayEnabled() const { return bDebugOverlay; }
	void SetAutoHitTestEnabled(bool bEnabled) { bAutoHitTestEnabled = bEnabled; }
	bool IsAutoHitTestEnabled() const { return bAutoHitTestEnabled; }

	bool ValidateBindings(TArray<FString>& OutErrors) const;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, Category = "RMSim|Binding")
	TObjectPtr<ARune> RedRune = nullptr;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, Category = "RMSim|Binding")
	TObjectPtr<ARune> BlueRune = nullptr;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, Category = "RMSim|Binding")
	TObjectPtr<AActor> FixedCamera = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "RMSim")
	ESceneMode CurrentMode = ESceneMode::Inactive;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "RMSim")
	int32 MatchSeed = 2026;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "RMSim|Test")
	bool bAutoHitTestEnabled = false;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "RMSim|Test", meta = (ClampMin = "0.0"))
	double AutoHitDelaySeconds = 1.0;

private:
	FRuneMotionModel MotionModel;
	bool bBindingsValid = false;
	FString BindingError;
	bool bDebugOverlay = false;
};
