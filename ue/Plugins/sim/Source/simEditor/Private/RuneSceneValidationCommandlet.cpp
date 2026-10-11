#include "RuneSceneValidationCommandlet.h"

#include "Engine/Level.h"
#include "Rune.h"
#include "RuneSceneDirector.h"

URuneSceneValidationCommandlet::URuneSceneValidationCommandlet()
{
	IsClient = false;
	IsEditor = true;
	IsServer = false;
	LogToConsole = true;
}

int32 URuneSceneValidationCommandlet::Main(const FString& Params)
{
	FString MapPath = TEXT("/Game/creo_stp");
	FParse::Value(*Params, TEXT("Map="), MapPath);
	UPackage* Package = LoadPackage(nullptr, *MapPath, LOAD_None);
	UWorld* World = Package ? UWorld::FindWorldInPackage(Package) : nullptr;
	if (!World)
	{
		UE_LOG(LogTemp, Error, TEXT("Unable to load map %s"), *MapPath);
		return 1;
	}

	TArray<ARuneSceneDirector*> Directors;
	if (World->PersistentLevel)
	{
		for (AActor* Actor : World->PersistentLevel->Actors)
		{
			if (ARuneSceneDirector* Director = Cast<ARuneSceneDirector>(Actor))
			{
				Directors.Add(Director);
			}
		}
	}
	if (Directors.Num() != 1)
	{
		UE_LOG(LogTemp, Error, TEXT("Expected one RuneSceneDirector, got %d"), Directors.Num());
		return 2;
	}

	TArray<FString> Errors;
	if (!Directors[0]->ValidateBindings(Errors))
	{
		for (const FString& Error : Errors)
		{
			UE_LOG(LogTemp, Error, TEXT("Rune binding: %s"), *Error);
		}
		return 3;
	}
	Directors[0]->RedRune->ValidateVisualProfile(Errors);
	Directors[0]->BlueRune->ValidateVisualProfile(Errors);
	if (!Errors.IsEmpty())
	{
		for (const FString& Error : Errors)
		{
			UE_LOG(LogTemp, Error, TEXT("Rune visual: %s"), *Error);
		}
		return 4;
	}

	UE_LOG(LogTemp, Display, TEXT("Rune scene validation passed for %s"), *MapPath);
	return 0;
}
