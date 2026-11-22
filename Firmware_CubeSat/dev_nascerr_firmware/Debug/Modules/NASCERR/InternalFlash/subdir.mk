################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (14.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.c 

OBJS += \
./Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.o 

C_DEPS += \
./Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.d 


# Each subdirectory must supply rules for building sources it contributes
Modules/NASCERR/InternalFlash/%.o Modules/NASCERR/InternalFlash/%.su Modules/NASCERR/InternalFlash/%.cyclo: ../Modules/NASCERR/InternalFlash/%.c Modules/NASCERR/InternalFlash/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m4 -std=gnu11 -g3 -DDEBUG -DUSE_HAL_DRIVER -DSTM32L4S5xx -c -I../Core/Inc -I../Modules/NASCERR/ -I../Modules/NASCERR/Bootloader -I../Modules/NASCERR/InternalFlash -I../Drivers/STM32L4xx_HAL_Driver/Inc -I../Drivers/STM32L4xx_HAL_Driver/Inc/Legacy -I../Drivers/CMSIS/Device/ST/STM32L4xx/Include -I../Drivers/CMSIS/Include -I../Middlewares/Third_Party/FreeRTOS/Source/include -I../Middlewares/Third_Party/FreeRTOS/Source/CMSIS_RTOS_V2 -I../Middlewares/Third_Party/FreeRTOS/Source/portable/GCC/ARM_CM4F -I../Modules/NASCERR/Application -I../Modules/NASCERR/Bootloader -I../Modules/NASCERR/Communication -I../Modules/NASCERR/Database -I../Modules/NASCERR/Experiments -I../Modules/NASCERR/ExternalFlash -I../Modules/NASCERR/Hardware -I../Modules/NASCERR/Sram -I../Modules/NASCERR/System -I../Modules/NASCERR/Utils -O0 -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-Modules-2f-NASCERR-2f-InternalFlash

clean-Modules-2f-NASCERR-2f-InternalFlash:
	-$(RM) ./Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.cyclo ./Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.d ./Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.o ./Modules/NASCERR/InternalFlash/NASCERR_InternalFlash.su

.PHONY: clean-Modules-2f-NASCERR-2f-InternalFlash

