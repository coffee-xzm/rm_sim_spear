#pragma once

#include "CoreMinimal.h"
#include "Engine/DataAsset.h"
#include "RuneVisualProfile.generated.h"

UCLASS(BlueprintType)
class SIM_API URuneVisualProfile : public UDataAsset
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> MetalMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> RedActiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> BlueActiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> CenterInactiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> RedCenterActiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> BlueCenterActiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> RedFanActiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> BlueFanActiveMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> RedArrowMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> BlueArrowMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> FullMaterial = nullptr;

	// Team-specific final materials. Runtime falls back to that team's active
	// material if a team-specific slot is not assigned. The legacy common
	// FullMaterial field is intentionally ignored by runtime selection.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> RedFullMaterial = nullptr;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material")
	TObjectPtr<UMaterialInterface> BlueFullMaterial = nullptr;

	// Index 0 maps to ring 1 and index 9 maps to ring 10.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material", meta = (TitleProperty = "Ring"))
	TArray<TObjectPtr<UMaterialInterface>> RedFanRingMaterials;

	// Index 0 maps to ring 1 and index 9 maps to ring 10.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Rune|Material", meta = (TitleProperty = "Ring"))
	TArray<TObjectPtr<UMaterialInterface>> BlueFanRingMaterials;
};
