#pragma once

#include "CoreMinimal.h"
#include "Subsystems/WorldSubsystem.h"
#include "RuneSnapshot.h"
#include "RMSimWorldSubsystem.generated.h"

class ARuneSceneDirector;

UCLASS()
class SIM_API URMSimWorldSubsystem : public UWorldSubsystem
{
	GENERATED_BODY()

public:
	virtual void OnWorldBeginPlay(UWorld& InWorld) override;

	void RegisterDirector(ARuneSceneDirector* InDirector);

	UFUNCTION(BlueprintCallable, Category = "RMSim")
	bool SetSceneMode(ESceneMode NewMode, int32 Seed = 2026);

	UFUNCTION(BlueprintCallable, Category = "RMSim")
	bool ResetMatch(int32 Seed = 2026);

	UFUNCTION(BlueprintCallable, Category = "RMSim")
	bool SetDebugOverlay(bool bEnabled);

	UFUNCTION(BlueprintCallable, Category = "RMSim")
	bool SetAutoHitTestEnabled(bool bEnabled);

	UFUNCTION(BlueprintPure, Category = "RMSim")
	FRunePairSnapshot GetSnapshot() const;

	UFUNCTION(BlueprintPure, Category = "RMSim")
	ARuneSceneDirector* GetDirector() const { return Director.Get(); }

	UFUNCTION(BlueprintPure, Category = "RMSim")
	bool IsReady() const { return Director != nullptr && RegistrationError.IsEmpty(); }

private:
	UPROPERTY()
	TObjectPtr<ARuneSceneDirector> Director = nullptr;

	FString RegistrationError;
};
